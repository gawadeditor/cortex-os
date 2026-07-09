# Product Experience System — Phase B5.25 Design

**Status:** Design only — no implementation  
**Phase:** B5.25 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B5 (Dashboard Architecture System), dashboard-surface-area.md  
**Feeds into:** B5.3 (Dashboard Director System), B4.95 (UX/UI Designer role)  
**Author:** Factory OS Architect

---

## 1. What the Product Experience System Is

Phase B5 defined what the dashboard contains — 15 pages, their data, their actions, their permissions.

Phase B5.25 defines how the dashboard behaves, guides, constrains, and protects the operator — the governance layer that sits above every component, every flow, and every decision about how the product feels.

The PES is not a visual design guide. It is not a color palette. It is not a component library. It is the **ruleset that governs all of those** — the system that answers: how do we make decisions about this product? What do we allow? What do we prevent? What must every page, every alert, every approval share in common?

The PES has three failure modes it must prevent:

1. **Dashboard sprawl** — more pages, more nav items, more controls than the operator needs
2. **Operator confusion** — workflows that do not match how operators think about their work
3. **Unsafe experiences** — flows that allow dangerous actions without adequate protection

Everything in this document is written to prevent one or more of these failures.

---

## 2. Product Experience Architecture

### 2.1 How the PES Connects the Product

```
┌─────────────────────────────────────────────────────────────────────┐
│  PRODUCT EXPERIENCE SYSTEM (B5.25)                                  │
│                                                                       │
│  The ruleset. Defines principles, standards, constraints, metrics.   │
│  Does not implement. Does not build. Governs.                        │
└──────────┬────────────┬───────────────┬────────────────┬────────────┘
           │            │               │                │
           ▼            ▼               ▼                ▼
    UX/UI Designer   Dashboard      Engineering      QA Engineer
    (B4.95)          Director       Director         (B4.95)
                     (B5.3)         (B4.9)
    Applies PES      Surfaces       Executes          Validates
    standards to     PES violations  PES-approved      PES compliance
    component        as UX debt.     changes.          in tests.
    specifications.  Enforces nav                      Checks
    Proposes PES     sprawl rules.                     accessibility,
    additions.                                         density, flows.
           │
           ▼
    Frontend Engineer (B4.95)
    Implements specifications produced by UX/UI Designer.
    Never deviates from PES without a spec change.
           │
           ▼
    Dashboard (B5)
    The 15 V1 pages, rendered per PES standards.
           │
           ▼
    White-label Factory Branding (§13)
    Factory-specific tokens applied at render time.
    PES is invariant. Branding is a token overlay.
```

### 2.2 Authority and Ownership

| Decision | Owner |
|---|---|
| What the PES rules ARE | PES (this document) |
| Whether a new design conforms to PES | UX/UI Designer (cites PES rule) |
| Whether a PES addition is needed for a new pattern | UX/UI Designer (proposes) → PES (adopts) |
| Whether implementation conforms to spec | QA Engineer |
| Whether a PES violation is a factory health issue | Dashboard Director |
| Whether a PES change should be engineered | Engineering Director |

**PES is not owned by any single role.** It is produced here as a design document. Changes require a design revision — not a code change. Any role can propose a PES change; the PES is authoritative until revised.

### 2.3 Relationship to Factory Branding

The PES defines the product experience. Factory branding applies a visual layer on top of it. The distinction is:

- **PES governs:** layout rules, information density, component behavior, flow logic, safety constraints, accessibility, error patterns, empty states, approval patterns, alert patterns
- **Factory branding governs:** logo, product name, accent color, dashboard title, support email

PES rules remain constant across all factory instances. Branding tokens change per instance. A PES rule cannot be overridden by branding. A branding token cannot change the layout, flow, or behavior of any component.

---

## 3. Operator Personas

Every PES decision must be made with a specific operator in mind. There are six personas, each with distinct goals, permissions, and failure points.

---

### Persona 1 — Owner

**Who they are:** The factory operator who owns the installation. May be the founder, technical lead, or agency director. Has full authority over all factory decisions.

**Goals:**
- Know the factory is running correctly without checking every detail
- Configure factory-level policy once, then leave it alone
- Handle rare high-stakes decisions: archive a channel, disable a module, grant admin access

**Default dashboard needs:**
- Home page with an accurate health score and any critical pending actions
- Cost visibility at a glance (monthly spend vs. ceiling)
- Channel statuses at a glance
- Escalation surface for anything that needs Owner-only authority

**What they should NOT see as default:**
- Agent-level performance metrics (irrelevant to factory-level decisions)
- Raw config inspector data (diagnostic tool, not a management view)
- Every individual gate flag from every episode (that detail belongs to Operators)

**Likely failure points:**
- Accidentally archives a channel when they meant to pause it (mitigation: distinct action labels, confirmation dialogs that state consequences)
- Misreads the health score as "everything is fine" when there are warnings (mitigation: warnings are visible on Home even when the score is high)
- Overlooks a credential expiring in 5 days because the health score is still above 80 (mitigation: critical items in Pending Actions regardless of score)

---

### Persona 2 — Admin

**Who they are:** The day-to-day factory manager. Handles configuration, user management, module oversight, model policy, and provider management. May be a technical producer or operations lead.

**Goals:**
- Manage channels and their configurations
- Handle approval backlogs (model swaps, budget requests, template upgrades)
- Maintain credentials and module health
- Configure new channels when expanding

**Default dashboard needs:**
- Home with health score and approval backlog
- Channel list with configuration status
- Approval queue (model swaps, module upgrades, budget requests)
- Credentials health

**What they should NOT see:**
- Danger zone actions (archive factory — Owner only)
- Client-scoped views (they see all channels)

**Likely failure points:**
- Configures a channel's budget ceiling too low, then wonders why runs keep failing (mitigation: budget configuration shows estimated run count before saving)
- Approves a model swap without reviewing the evidence (mitigation: evidence is displayed inline with every approval, requires scroll/expand before [Approve] is enabled)
- Leaves pending approvals to accumulate until they're aged (mitigation: approval age shown in the approval panel, alert triggered at 24h)

---

### Persona 3 — Operator

**Who they are:** The production manager. Their job is to keep the episode pipeline running — queue episodes, review gate results, approve publishing, investigate failures.

**Goals:**
- Queue episodes without configuration friction
- Know what is currently running and how it's going
- Review gate results and approve episodes for publishing
- Diagnose failures and retry runs

**Default dashboard needs:**
- Production queue with live run status
- Review queue with pending approvals
- Home alerts for blocked runs or failed gates

**What they should NOT see as default:**
- Credential management (irrelevant to their role)
- Factory-level policy controls
- Template library (unless also responsible for channel creation)
- Agent performance metrics (advanced, diagnostic)

**Likely failure points:**
- Approves an episode without reading the gate flags (mitigation: flag count is prominently displayed; flags require a visible expand before [Approve] is accessible)
- Retries a failed run without understanding why it failed (mitigation: [Diagnose] action shows the failure in detail before [Retry] is available)
- Queues a run on a paused channel (mitigation: the channel selector only shows active channels; paused channels are disabled in the dropdown with a label explaining why)

---

### Persona 4 — Editor

**Who they are:** A content contributor or brand manager who needs to review channel profile settings and read gate verdicts, but does not control production.

**Goals:**
- Review and suggest edits to channel brand identity, tone, and content pillars
- Read gate verdict results to understand brand compliance status
- View episode history (read-only)

**Default dashboard needs:**
- Channel profile (brand, tone, visual identity, content pillars) — edit mode for their assigned channels
- Review page (read-only verdict history)
- Home (simplified — no cost data, no policy controls)

**What they should NOT see:**
- Cost data (production spend is not their concern)
- Credential management
- Model/provider assignments
- Any approval actions (they can read verdicts, not approve)
- Factory settings

**Likely failure points:**
- Makes a channel profile edit without understanding it affects the next run immediately (mitigation: edit confirmation shows "This change will take effect on the next episode run.")
- Sees the [Activate Channel] button and clicks it without knowing what it means (mitigation: activation is Admin-level — Editors see [Activate] as disabled with tooltip "Activation requires Operator access or above")

---

### Persona 5 — Viewer

**Who they are:** A stakeholder, investor, or client contact who needs read access to factory output without any ability to change anything.

**Goals:**
- See that the factory is producing content
- See basic performance indicators
- Review recent episodes and their status

**Default dashboard needs:**
- Home (simplified — channel status, recent completions, no cost data, no health score detail)
- Analytics (if enabled — their channels only)
- Production (read-only run history — their channels only)

**What they should NOT see:**
- Any cost data
- Any configuration controls
- Any approval queues
- Credentials
- Settings
- Factory health score (this is an operator tool, not a stakeholder view)
- Gate verdict flags (aggregate pass/fail only)

**Likely failure points:**
- Sees a run listed as "awaiting approval" and assumes publishing is broken (mitigation: status label "awaiting operator review" with a tooltip explaining what approval means)
- Confused by technical terminology in run status labels (mitigation: Viewer-facing labels use plain language: "in progress," "review needed," "published," "failed — being investigated")

---

### Persona 6 — Client

**Who they are:** An agency client who has been given access to their specific channel(s) in the factory. Does not know or care that this is "Factory OS" — they see the factory's branded dashboard.

**Goals:**
- See episodes being produced for their brand
- Review recent content and approval status
- Basic performance visibility for their channel

**Default dashboard needs:**
- Home (scoped to their channels only — factory branding from white-label settings)
- Channels (their channels only — read + profile review)
- Production (their channels only — run history, no queue actions)
- Review (their channels only — read, no approval actions)
- Analytics (if enabled — their channels only)

**What they should NOT see:**
- Any other client's data
- Factory settings
- Credentials
- Template library
- Other channels
- Module management
- Any hint that this is a multi-tenant platform

**Likely failure points:**
- Notices the factory name "Hypogeum Media Factory" in page titles and wonders what it is (mitigation: Client role sees `factory_branding.dashboard_title` only — not the factory entity name unless it matches the brand)
- Sees a channel in "paused" status and panics (mitigation: Client-facing status labels are friendlier: "paused" → "on hold" with a tooltip the agency can pre-configure)

---

## 4. Experience Principles

These twelve principles are applied to every page, every component, every flow. When a design decision is made, it must be defensible against one or more of these principles.

---

**P1 — Progressive Disclosure**  
Show the minimum required to complete the immediate task. Reveal detail on demand. A 10-step wizard is not shown all at once — each step is one decision. A channel detail page shows summary first, full profile on tab expansion. Raw config is in the Config Inspector, not the channel card.

*Test: What does a first-time operator see? If it requires understanding 5+ concepts to proceed, the design fails progressive disclosure.*

---

**P2 — Simple First-Run Setup**  
The first time an operator logs in, they should be able to set up a working factory and queue their first episode within 30 minutes without reading documentation. The onboarding wizard (§7) must be the first experience, not the full dashboard. Advanced configuration is deferred until after the first successful run.

*Test: Can a non-technical factory operator complete initial setup in under 30 minutes following only in-dashboard prompts?*

---

**P3 — Advanced Controls Hidden by Default**  
The Advanced nav section is collapsed by default. Advanced page content defaults to summary views, not expanded configuration. Model assignments, provider routing, and config inspection are power-user tools — they are available but not prominent.

*Test: Does the sidebar have more than 6 primary items visible on login? If yes, P3 is violated.*

---

**P4 — Explain Before Configure**  
Every form field that affects behavior must explain what it controls before the operator edits it. Not a tooltip — a sentence. Not in documentation — inline. Budget ceiling fields show: "Runs that would exceed this cost are blocked." Approval threshold fields show: "This determines how strictly brand rules are enforced during review."

*Test: Can an operator configure any setting without knowing what it does? If yes, P4 is violated.*

---

**P5 — Dangerous Actions Require Confirmation**  
Any action that is hard to reverse, affects shared systems, or could cause loss of data or access must include:
1. A warning that clearly states the consequence
2. A confirmation step that is not a single click (typed slug for archive actions, checkbox + confirm for irreversible deletions)
3. No auto-confirm timer — the operator must actively confirm

*Test: What is the minimum number of clicks to archive a channel? If less than 3, P5 is violated.*

---

**P6 — Every Alert Suggests an Action**  
An alert without a resolution path is noise. Every alert — in the Home Pending Actions section, in sidebar badges, in toast notifications — must include a specific suggested action with a deep link to where that action can be taken.

"Gmail credential expires in 5 days" is not a complete alert.  
"Gmail credential expires in 5 days. [Renew credential →]" is.

*Test: Does any alert in the dashboard lack a suggested action? If yes, P6 is violated.*

---

**P7 — Every Empty State Explains the Next Step**  
A blank page, empty table, or empty list is a failure mode. Every empty state must tell the operator: what this section shows when populated, why it is empty now, and what they should do to populate it.

*Test: Does any empty state lack a next step? If yes, P7 is violated.*

---

**P8 — One Place Per Action**  
Every write action lives on exactly one page. No action is duplicated across pages. Pausing a channel is done on the Channels page — not from the Production page, not from Home. This prevents state inconsistency and reduces cognitive load.

*Test: Is any write action available from more than one page? If yes, P8 is violated.*

---

**P9 — Status Must Be Unambiguous**  
Every entity status indicator (channel active/paused, run running/failed, module enabled/degraded) must be: legible in text (not color alone), specific (not "error" — "failed at stage VPD: budget exceeded"), and actionable (status degraded → link to fix).

*Test: If a colorblind operator reads the dashboard, do they know the status of every entity? If not, P9 is violated.*

---

**P10 — The Dashboard Never Surprises**  
Side effects of actions are always disclosed before the action is taken. Activating a channel does not silently queue a run. Upgrading a module does not silently restart processes. Approving a model swap takes effect on the next run, not the currently running one. Every confirmation dialog states when the change takes effect.

*Test: After any write action, is there any system behavior the operator was not warned about? If yes, P10 is violated.*

---

**P11 — Reduce Before You Complexity**  
Before adding a new setting, a new form field, a new page section — reduce. Can the same goal be achieved with fewer controls? Is this control covering for a missing default? Is this a power-user concern that belongs in Advanced? Complexity is added only when the operator cannot succeed without it.

*Test: What is the simplest form this page could take that still serves the operator task? If the current design is more complex than that, P11 is being ignored.*

---

**P12 — Errors Are Teaching Moments**  
An error message is not a failure — it is a guide. Error messages name the specific problem, state what the operator can do about it, and link to the relevant fix path. "An error occurred" violates P12. "MUAPI episode budget exceeded (needed 6.2 credits, limit is 5.0). [Adjust budget in Channel Settings →]" does not.

*Test: Does any error message in the dashboard lack a specific cause and a resolution path? If yes, P12 is violated.*

---

## 5. Information Density Rules

These rules govern how much information is shown in each context. Information density failures are the primary cause of operator confusion.

---

### Rule ID1 — Primary Cards per Page

No page may show more than **8 primary cards or list items** before pagination or progressive loading. "Primary card" means a top-level content unit that requires the operator to process it individually (e.g., a channel card, a run card, a recommendation card).

Exception: Tables are not subject to the 8-item limit — they paginate at 25 rows. The distinction: a card demands attention; a table row is scanned.

---

### Rule ID2 — Visible Actions per Entity

No entity card may surface more than **4 visible actions**. If more actions exist, secondary actions collapse into a `[⋯ More]` menu. Primary actions (the most likely next step) are prominent; secondary actions are available but not visible by default.

Examples of correct action hierarchy:
- Channel card: [View] [Clone] [⋯ More: Pause, Archive, Export]
- Run card: [View Live Log] [⋯ More: Cancel, Clone Config]
- Credential card: [Test] [Edit] [⋯ More: Revoke, Delete]

---

### Rule ID3 — Section Collapse

A page section must be collapsed by default if:
- It is not needed for the operator's most common task on this page
- It contains more than 6 fields or items
- It is targeted at a specific sub-role of the page's audience

On the Channel Detail page: the Configuration tab is collapsed by default (most operators edit profile, not models). On the Settings page: the Danger Zone is collapsed by default.

---

### Rule ID4 — Tab Usage

Use tabs when:
- A page has 3 or more distinct content areas that serve different sub-tasks
- Content areas are approximately equal in importance (no single dominant area)
- Switching between content areas does not require saving state

Do not use tabs when:
- There are only 2 content areas (use a section divider instead)
- The content areas have a required order (use a wizard step instead)
- One content area is much more important than the others (show the primary content, collapse the secondary)

---

### Rule ID5 — Drawers and Modals

Use a **drawer** (side panel that slides in) when:
- Showing detail for an item in a list without leaving the list context
- The operator needs to compare the detail view with other list items
- The action inside is read-only or a quick single-field edit

Use a **modal** (centered dialog) when:
- Requiring explicit confirmation before a consequential action
- Collecting a small amount of input (1–4 fields) before an action
- Showing a critical warning that must be acknowledged before proceeding

Do not use modals when:
- The content requires more than 4 form fields (use a wizard or page instead)
- The modal would need to open another modal (never nest modals)
- The content is purely informational and does not require an action (use a drawer or inline expansion)

Maximum modal depth: **1**. No modal may open another modal.

---

### Rule ID6 — Split into Advanced Page

Split content to an Advanced page when:
- Fewer than 20% of operators will ever need this content during normal operation
- The content requires domain expertise that general operators do not have
- The content is diagnostic rather than operational (inspecting resolved config, viewing benchmark results)
- The content changes infrequently (module registry, provider policy, user roles)

The test: if a new operator never visits this content in their first 10 production runs, it belongs in Advanced.

---

## 6. Navigation UX Rules

These rules enforce the navigation constraints defined in `dashboard-surface-area.md` and `dashboard-architecture-system.md`.

---

**N1 — Six Primary Nav Items Maximum**  
The primary navigation section must never exceed 6 items. If a new page is proposed for primary nav, an existing item must be demoted to Advanced or the new page must be added to Advanced or Module sections instead.

The six primary items are fixed: Home, Channels, Production, Review, Credentials, Settings. Any proposal to add a seventh primary item requires a formal scope change (updating `dashboard-surface-area.md`).

---

**N2 — No Duplicate Destinations**  
Two nav items must never navigate to the same page, the same page with different filters, or pages that contain each other's content. If two nav items serve overlapping purposes, they must be merged or one must be eliminated.

---

**N3 — Module Nav Items Are Gated**  
A module nav item appears if and only if the backing module is enabled. A disabled module nav item does not appear as greyed-out — it does not appear at all. An operator who has never enabled a module does not know that module nav item exists.

Exception: on the Modules advanced page, all available modules are listed — enabled and disabled. This is the management surface, not the operational surface.

---

**N4 — Advanced Nav Is Collapsed by Default**  
On every login, the Advanced nav section starts collapsed. The section expands only when the operator explicitly expands it or navigates to an advanced page. Expansion state is preserved per session but resets on new login.

---

**N5 — Active Page Is Always Clear**  
The current page is always indicated in the nav. Sub-pages (channel detail, run detail) highlight their parent nav item. No nav item is highlighted unless the operator is on that page or a sub-page of it.

---

**N6 — No Nav Without a Page**  
Every nav item must navigate to a real page. "Coming soon," "under construction," and disabled nav items that navigate nowhere are prohibited. Future pages are not in the nav. If a module is not built, its nav item does not exist.

---

**N7 — Deep Links Are Stable**  
Every dashboard page must be reachable by URL. Sharing a URL to a specific channel detail, a specific run, or a specific approval must work. Pages that require navigation state (multi-step wizards in progress) are not deep-linkable for their intermediate steps — only their entry point and completion state.

---

## 7. Component System Standards

These are design-level standards for each component type. They define the contract that UX/UI Designer specifications must honor and Frontend Engineer implementations must satisfy.

---

### C1 — Cards

**Purpose:** Surface a single entity (channel, run, template, credential) in a list or grid.

**Required elements:**
- Entity name (largest text element on the card)
- Entity status badge (see C8)
- Primary data points: maximum 3 visible at once
- Primary action(s): maximum 2 visible, additional actions in [⋯ More] menu

**Prohibited elements:**
- No nested cards
- No more than 4 lines of visible text per card without a [Show more] control
- No inline editing — editing opens the detail view or a dedicated form

**State requirements:** Every card must handle: default, hover, loading, error (entity unavailable), and empty (entity has no data yet).

---

### C2 — Tables

**Purpose:** List multiple entities of the same type with sortable columns.

**Required elements:**
- Column headers (sortable by clicking header)
- Pagination controls (shown when >25 rows)
- Row hover state (highlight row on hover)
- Per-row actions: maximum 2 visible, [⋯] for additional

**Prohibited elements:**
- No nested tables
- No more than 8 columns without horizontal scroll
- No column that is always empty (remove the column if data never exists)

**Sorting:** Default sort must be stated explicitly in the component spec. Clicking a column header sorts; second click reverses. No multi-column sort in V1.

**Empty state:** When a table has no rows, show the empty state component (C7) — not an empty table grid.

---

### C3 — Forms

**Purpose:** Collect operator input for create or update operations.

**Required elements:**
- Field label above the input (not inside as placeholder — placeholder disappears on input)
- Field description / help text below the label (inline, not on hover)
- Validation error message below the field (inline, immediate on blur, not on submit only)
- Save / Cancel actions at form bottom (or per-section in multi-section forms)

**Prohibited elements:**
- No required field indicated by color alone (always include the asterisk + text "required")
- No inline edit that activates by accidentally clicking text
- No form that submits on Enter by default if it contains multiple fields

**Validation rules:**
- Validate on blur (when operator moves away from a field), not on keystroke
- Show all validation errors simultaneously after first submit attempt
- Never clear a valid field when showing another field's error

---

### C4 — Wizards

**Purpose:** Guide the operator through a multi-step process (channel creation, first-run setup, clone flow).

**Required elements:**
- Step indicator: "Step 3 of 10 — Audience" (number + title, always visible)
- [Back] button available from step 2 onward
- [Next] button disabled until current step is valid
- [Save draft] capability if the wizard takes more than 5 minutes
- [Cancel] with explicit warning: "Your progress will be lost. Are you sure?"

**Step rules:**
- Maximum 10 steps per wizard
- Each step covers one logical decision (not multiple unrelated decisions)
- Required fields in a step must be completed before [Next] is enabled
- Optional fields in a step may be skipped — a completion indicator (% complete) updates accordingly

**Prohibited patterns:**
- No wizard within a wizard
- No step that requires the operator to navigate away from the wizard to complete

---

### C5 — Alerts

**Purpose:** Surface time-sensitive issues requiring operator attention.

**Alert levels:**
- `INFO` (blue) — informational, no action required
- `WARNING` (amber) — action recommended, not urgent
- `CRITICAL` (red) — action required before next run or before a deadline
- `BLOCKED` (red, distinct from critical) — production is actively prevented until this is resolved

**Required elements (every alert):**
- Level indicator: color + icon + text label (not color alone — P9)
- Title: one sentence stating the problem
- Suggested action: one sentence stating what to do
- Action link: a deep link to where the action can be taken
- Dismissal: explicit or implicit (resolving the issue dismisses the alert)

**Prohibited patterns:**
- No alert without a suggested action (P6)
- No alert stack of more than 5 items — if more than 5 alerts are active, the top 5 by priority are shown and a summary is provided: "and 3 more [View all]"
- No permanent alerts — every alert must have a dismissal condition

---

### C6 — Approval Panels

**Purpose:** Present a pending approval decision to the operator with full context.

**Required sections (every approval):**
1. **What changes:** plain-language description of exactly what will be different after approval
2. **Why it was recommended:** the evidence or trigger (e.g., "Cost Intelligence Agent detected 22% cost reduction opportunity over 30 runs")
3. **Risk level:** LOW / MEDIUM / HIGH / CRITICAL badge
4. **Affected entities:** which channels, agents, or modules are affected
5. **Rollback plan:** what happens if this needs to be undone (if applicable)
6. **Evidence link:** link to the full recommendation evidence (for operators who want detail)
7. **Actions:** [Approve] [Reject] [Defer 7 days]

**Approval gating:**
- HIGH risk and CRITICAL risk approvals: [Approve] is disabled until the operator has scrolled through all sections (scroll position tracked by the frontend)
- LOW risk: [Approve] is immediately available but evidence sections are pre-expanded

**Rejection:** Rejection always requires a reason (short dropdown: "Evidence insufficient" / "Wrong timing" / "Disagree with recommendation" / "Other" + free text). Reason is stored in the approval record.

**Defer:** Deferral postpones the approval for 7 days (configurable). Deferred approvals re-surface in the approval queue after the deferral period. Maximum 2 deferrals per recommendation before the system requires a final decision.

---

### C7 — Empty States

**Purpose:** Replace blank page areas with useful context when no data exists.

**Required elements:**
- Icon (relevant to the content type — not a generic "no data" icon)
- Heading: what this section shows when populated
- Body: why it is empty now (new factory / no runs yet / module disabled / etc.)
- Call to action: the specific next step, with a button or link

**Example (Production page, no runs):**

```
  [▶] Queue icon

  No episodes in production yet

  Start your first episode run to see production history,
  live stage progress, and cost tracking here.

  [Queue your first episode →]
```

**Empty state must NOT be:**
- An empty table grid (visible column headers with no rows)
- An empty chart (axis lines with no data)
- A blank white space with no explanation

---

### C8 — Status Badges

**Purpose:** Indicate the current state of an entity in a compact, readable format.

**States and colors (with text alternative for color-blind operators):**

| Status | Color | Text | Icon |
|---|---|---|---|
| active | green | ● active | filled circle |
| paused | grey | ○ paused | empty circle |
| archived | grey, italic | ○ archived | empty circle |
| running | blue, animated | ◉ running | pulsing circle |
| queued | blue | ◌ queued | dashed circle |
| completed | green | ✓ completed | checkmark |
| failed | red | ✗ failed | X mark |
| awaiting approval | amber | ⚠ review needed | warning triangle |
| enabled | green | ● enabled | filled circle |
| disabled | grey | ○ disabled | empty circle |
| degraded | amber | ◐ degraded | half-circle |
| expiring | amber | ⚠ expiring | warning triangle |
| expired | red | ✗ expired | X mark |

**Rules:**
- Never use color as the only status indicator (P9 — each status has a text label and icon)
- Badge text must be readable at 14px minimum
- No animated badges except `running` (animation indicates active process, not urgency)

---

### C9 — Health Indicators

**Purpose:** Show the aggregate health of a system, module, or factory in a compact, scannable form.

**Health score display:**
```
Health: 87/100  ●●●●●●●●○○
```
- Score displayed as number (87) and denominator (100)
- Bar shows proportional fill
- Color: 80+ = green, 60–79 = amber, 0–59 = red

**Health status display (per-module, per-credential):**
```
Analytics      ● healthy
Archive        ● healthy
Reuse          ◐ degraded    [View issue]
Notifications  ● healthy
Learning       ○ disabled
```

**Rules:**
- Degraded states always include a [View issue] link
- Disabled states do not show as "unhealthy" — disabled is intentional, not broken
- Health indicator does not include a percentage unless the health score system backs it

---

### C10 — Credential Fields

**Purpose:** Collect and display credential information with maximum safety.

**Input rules:**
- Credential value field type: `password` (masked, no show/hide toggle)
- Label field: `text` (the credential's human-readable name — e.g., "openrouter-prod")
- No auto-fill enabled on credential value fields (autocomplete="new-password" attribute)
- Placeholder text: "Enter [provider name] API key" — never a sample value

**Display rules:**
- After saving: the value is never shown. The field shows "••••••••••••" as a visual placeholder
- Edit form: the value field is blank — not pre-populated with the current value
- List view: provider, label, type, and status only — no value, no partial value

**Security rules:**
- The credential form never submits in the same request as any other form data
- Credential submission is always a standalone POST to `/credentials`
- Browser dev tools inspection must not reveal the credential value (enforced by the Backend API — credentials are not in page state after submission)

---

### C11 — Secret Fields

**Same rules as Credential Fields (C10).** Secret fields are used for any sensitive value: API key, token, webhook secret, OAuth client secret. The rules apply uniformly regardless of the context.

Additional rule: A secret field confirmation step is required when the value being entered is used in a live production context. The confirmation step shows: "This credential will be used for [purpose]. Test it before saving? [Test Now]"

---

### C12 — Confirmation Dialogs

**Purpose:** Protect operators from irreversible or high-impact actions.

**Levels:**

**Level 1 — Standard Confirmation** (for reversible actions with significant effects):
```
[Modal]
Pause Channel: The Hypogeum Chronicles

Pausing this channel will stop all future runs.
Active runs will complete. This can be undone.

[Cancel]  [Pause Channel]
```

**Level 2 — Acknowledged Confirmation** (for harder-to-reverse actions):
```
[Modal]
Archive Channel: The Hypogeum Chronicles

This channel will be archived. All runs will stop.
The channel becomes read-only.
This can be undone only by an Owner.

I understand this channel will be archived.
[☐] Check to confirm

[Cancel]  [Archive Channel]  ← disabled until checkbox checked
```

**Level 3 — Typed Confirmation** (for irreversible or factory-critical actions):
```
[Modal]
Archive Factory: Hypogeum Media Factory

This will stop all runs, pause all channels, and make
this factory read-only. This cannot be undone.

Type the factory slug to confirm:
[_______________]  ← must match "hypogeum" exactly

[Cancel]  [Archive Factory]  ← disabled until slug typed correctly
```

**Rules:**
- Level 1 for: pause channel, remove user, revoke credential
- Level 2 for: archive channel, delete template, disable module
- Level 3 for: archive factory, delete factory data
- [Cancel] always on the left; destructive action always on the right
- Destructive action button is styled as danger (red) — not primary (accent color)
- No "don't show again" checkbox on any confirmation dialog

---

### C13 — Rollback Panels

**Purpose:** Surface rollback status and controls for changes that are in a monitoring window.

**Required elements:**
- What was changed (plain language)
- When it was applied
- Current monitoring status: time elapsed, metrics being watched, current values vs. thresholds
- [Trigger Rollback] button (Dangerous — Level 1 confirmation required)

**Display in context:** Rollback panels appear inline on the relevant page (the entity that was changed). An active rollback panel on the Channels page for a channel that had a model swap means the channel detail shows the panel.

**Rollback button rules:**
- Always visible during the monitoring window
- Shows remaining monitoring window time: "3 of 5 required runs completed"
- After monitoring window closes (confirmed): panel disappears
- After rollback: panel shows rollback status and is read-only

---

## 8. Onboarding UX

The first-run setup flow is triggered automatically when a factory has no completed episodes and the owner logs in for the first time. It is not a forced wizard — the operator can dismiss it at any time and return later via Home → [Resume Setup]. The progress is preserved.

### Setup Flow (7 steps)

**Step 1 — Factory Setup** `→ Settings`

```
Welcome to [Factory Name]

Let's get your factory ready to produce.
This takes about 15 minutes.

STEP 1 OF 7  —  Factory identity

Factory name:     [___________________]  ← pre-filled from install
Factory slug:     [___________________]  ← set once, immutable
Default language: [English ▾]
Timezone:         [UTC ▾]

These settings affect how your factory is identified
and how timestamps are displayed throughout the dashboard.

[Save & Continue →]  [Skip for now]
```

---

**Step 2 — Credentials** `→ Credentials`

```
STEP 2 OF 7  —  Connect your production services

To produce episodes, your factory needs API access to:

  ☐ OpenRouter       LLM routing (required for all agent calls)
  ☐ MUAPI            Image and video generation (required for visual production)
  ☐ ElevenLabs       Narration voice synthesis (required for audio)
  ☐ Perplexity       Research and fact sourcing (required for research stage)
  ☐ YouTube          Publishing completed episodes (required for publishing)

Click each service to add its API key. Test each one before continuing.

[OpenRouter ✗ not connected]  [MUAPI ✗]  [ElevenLabs ✗]
[Perplexity ✗]  [YouTube ✗ OAuth required]

[← Back]  [Continue →]  ← enabled when all required credentials are tested
```

---

**Step 3 — Create or Clone Channel** `→ Channel Builder`

```
STEP 3 OF 7  —  Define your first channel

A channel is who your factory produces for.
Every episode, every brand rule, every platform connection
belongs to a channel.

How would you like to start?

[Start from a Template]      [Clone an Existing Channel]
Browse 6 production-ready    Copy a channel you've already
templates for different      configured in another factory
content types.               (requires a channel export file).
  ↓                            ↓
[→ Open Template Library]    [→ Import Channel File]
```

If starting from template → opens Channel Builder wizard at Step 1 (Select Template).

---

**Step 4 — Channel Profile Completion** `→ Channel Detail → Profile tab`

```
STEP 4 OF 7  —  Complete your channel profile

Profile completion:  ████████████░░  87%

Your channel needs a few more details before it's ready.

REQUIRED (2 remaining):
  ✗ narration_voice_id     Who narrates your episodes?
    [→ Assign ElevenLabs voice]

  ✗ platform credentials   Which YouTube channel do episodes publish to?
    [→ Connect YouTube channel]

OPTIONAL (4 remaining):
  ○ audience_personas      Define your viewer archetypes
  ○ competitors            Track competing channels
  ○ monetization_goals     Set revenue targets
  ○ learning_goals         Configure what the factory tracks

[← Back]  [Skip optional items]  [Continue →]  ← enabled when required items complete
```

---

**Step 5 — Provider Readiness** `→ Advanced → Providers & Models`

```
STEP 5 OF 7  —  Confirm your production providers

Your channel is configured to use:

  LLM:        OpenRouter → claude-sonnet-4-6       ✓
  Image:      MUAPI → flux-pro                     ✓
  Video:      MUAPI → veo3-fast-text-to-video      ✓
  Narration:  ElevenLabs → [voice selected]        ✓
  Research:   Perplexity → sonar-pro               ✓

These are your template defaults. You can change them later
in Advanced → Providers & Models.

[← Back]  [These look right, continue →]
```

---

**Step 6 — Production Readiness** `→ Channels → Readiness tab`

```
STEP 6 OF 7  —  Ready to produce

Your channel is ready to run.

  ✓ Channel profile complete
  ✓ All required credentials connected
  ✓ Narration voice assigned
  ✓ YouTube channel connected
  ✓ Budget ceiling configured ($15.00 per episode)
  ✓ Approval rules configured (human review: ON)

Channel status: PAUSED
You need to activate this channel before it can produce episodes.

[← Back]  [Activate Channel & Continue →]
```

---

**Step 7 — Review Readiness** `→ Review`

```
STEP 7 OF 7  —  Your first episode

Your factory is set up. Here's what happens when you run an episode:

1. Research Director → finds sources and builds a research brief
2. Narrative Architect → plans the story structure
3. Script Engine → writes the episode script
4. Brand Guardian → checks brand compliance (Pass 1)
5. Visual Package Director → plans and generates scenes
6. Brand Guardian → checks visual compliance (Pass 2)
7. Audio / Narration → synthesizes narrator voice
8. Thumbnail Director → generates thumbnail options
9. QA → validates completeness
10. Review → YOU approve before publishing (required)
11. Publishing → uploads to YouTube

Your first run costs approximately $8–15.
Budget ceiling is $15.00.

[← Back]  [Go to Production → Queue my first episode]
```

After completing the setup flow, the operator lands on the Production page with the Queue Episode panel open. The onboarding wizard is marked complete. The Home page shows a "Setup complete" confirmation that disappears after first episode completion.

---

## 9. Approval UX Standards

Every approval in the dashboard — regardless of what is being approved — uses the same structural template. Consistency reduces cognitive load: once an operator knows how to read an approval, they know how to read any approval.

### 9.1 Approval Panel Structure

```
APPROVAL REQUEST
──────────────────────────────────────────────────────────────────────
TYPE:   Model Swap              RISK:  ● LOW
SOURCE: Cost Intelligence Agent DATE:  2026-06-13 09:14 UTC

WHAT CHANGES
  Research Director Agent
  Current model:   claude-sonnet-4-6   ($0.004/1k tokens)
  Proposed model:  claude-haiku-4-5    ($0.0008/1k tokens)

WHY IT WAS RECOMMENDED
  Cost Intelligence Agent observed 30 consecutive runs where Research
  Director output quality was rated ≥ 0.90 (gate pass rate: 100%,
  human override rate: 0%). The cheaper model is predicted to maintain
  quality at 18% of the current cost for this agent.
  Estimated monthly saving: $24.00

AFFECTED ENTITIES
  ● The Hypogeum Chronicles (Research Director model assignment)

ROLLBACK PLAN
  If post-migration metrics fall below threshold:
  gate_pass_rate < 0.90 or human_override_rate > 0.10
  → Auto-rollback to claude-sonnet-4-6 after 5 runs below threshold
  → Rollback takes effect on next run after trigger

EVIDENCE
  [View 30 run evidence window →]

PENDING SINCE:  4 hours   DEFER LIMIT: 2 of 2 remaining

[Approve]  [Reject ▾]  [Defer 7 days]
              → Evidence insufficient
              → Wrong timing
              → Disagree with recommendation
              → Other: [__________]
──────────────────────────────────────────────────────────────────────
```

### 9.2 Approval Types and Required Sections

| Approval Type | What Changes | Why Rec'd | Risk | Affected | Rollback | Evidence |
|---|---|---|---|---|---|---|
| Publish episode | Episode → Published | Gate results | n/a | Channel | Auto-unpublish if retracted | Gate verdicts |
| Model swap | Agent default model | Benchmark / cost | LOW–MED | Channels using agent | Auto-rollback on metrics | Run evidence |
| Provider swap | Capability routing | Benchmark | MED–HIGH | All channels | Routing restore | Benchmark results |
| Budget increase | Episode ceiling | Cost spike | MED | Channel | Revert ceiling | Cost history |
| Module upgrade | Module version | Upgrade available | MED–HIGH | All channels | Rollback to prior version | Changelog |
| Engineering change | Code / config | Approved rec | LOW–CRIT | Affected entities | Checkpoint + rollback plan | Change plan |
| Template upgrade | Channel config | Template new version | MED | Channel | Revert to prior config | Version diff |

### 9.3 HIGH/CRITICAL Risk Approval Gating

For HIGH and CRITICAL risk approvals:
1. The [Approve] button is initially disabled with tooltip "Read all sections to enable"
2. Each section has a scroll-tracking indicator (read state tracked per section)
3. After all sections are scrolled into view: [Approve] becomes enabled
4. An additional acknowledgment checkbox appears: "I understand this change carries [risk level] risk and have reviewed the evidence."

This gating rule applies even if the operator refreshes the page — gating resets per session.

---

## 10. Alert UX Standards

### 10.1 Alert Levels

**INFO** (blue, `ℹ`)
- What it is: non-urgent information the operator may want to know
- Example: "Archive module has a minor upgrade available (v1.1.0 → v1.2.0)"
- Urgency: none
- Suggested action: optional
- Dismissal: operator clicks dismiss; stays dismissed
- Escalation: none

**WARNING** (amber, `⚠`)
- What it is: an issue that will become a problem if not addressed
- Example: "Gmail credential expires in 12 days"
- Urgency: address within 7 days
- Suggested action: required
- Dismissal: resolving the issue (auto-dismissed when credential renewed)
- Escalation: escalates to CRITICAL if not resolved within the defined window

**CRITICAL** (red, `✗`)
- What it is: an issue requiring immediate action that may block production
- Example: "Gmail credential expires in 2 days — notifications will stop"
- Urgency: address today
- Suggested action: required, prominently displayed
- Dismissal: resolving the issue only (cannot be manually dismissed)
- Escalation: if not resolved after deadline, status becomes BLOCKED

**BLOCKED** (red with border, `⛔`)
- What it is: production is currently prevented due to this issue
- Example: "YouTube credential expired — publishing is blocked"
- Urgency: immediate
- Suggested action: required, single CTA (no other options until resolved)
- Dismissal: resolving the issue only
- Escalation: none (already at maximum severity)

### 10.2 Alert Sources

Every alert must have a defined source. The source is displayed in the alert (small, secondary text). Sources:

| Source | Alert Types Generated |
|---|---|
| Dashboard Director | Health score drops, configuration drift, pending approvals aged, channel misconfiguration |
| Module Manager | Module degraded, module upgrade available, dependency conflict |
| Credential Monitor | Credential expiring, credential failed test, OAuth token needs renewal |
| Finance Controller | Budget threshold approaching (>80%), monthly ceiling at risk |
| Run Registry | Active run failed, active run cost spike |
| Acceptance Audit | Pending approval aged >24h, gate override triggered |
| Learning Module | Auto-applied change (informational), recommendation deferred limit reached |

### 10.3 Alert Priority and Display Rules

- Home Pending Actions: shows alerts ordered by urgency (BLOCKED > CRITICAL > WARNING > INFO), then by age (oldest first within same level)
- Sidebar badges: shows count of BLOCKED + CRITICAL alerts on the Review nav item
- Toast notifications: for real-time events (run failed, approval needed). Toast displays for 8 seconds. Critical toasts do not auto-dismiss — operator must dismiss manually
- Alert deduplication: the same issue cannot generate more than one active alert. A credential expiring generates one alert per credential, not one per day

---

## 11. Empty State Standards

Each major dashboard area has a defined empty state. These are the required strings — implementations may add appropriate iconography.

| Location | Empty State |
|---|---|
| Channels list (no channels) | "No channels yet. Channels are the identities your factory produces for — each with its own brand, audience, and publishing targets. [→ Create your first channel]" |
| Channel runs tab (no runs) | "No episodes have been run for this channel. [→ Queue first episode in Production]" |
| Channel readiness (all items passing) | "This channel is fully configured. Ready to produce." |
| Production queue (nothing running or queued) | "Nothing in the production queue. Completed and failed runs appear below. [→ Queue an episode]" |
| Production completed (no completed runs) | "No completed runs yet. Runs appear here after they finish." |
| Production failed (no failures) | "No failed runs. (Good news.)" |
| Review pending (no pending approvals) | "No episodes awaiting approval. All caught up." |
| Review history (no history) | "Gate verdict history appears after your first complete episode run." |
| Analytics (module enabled, no data) | "Analytics data appears 48 hours after your first published episode. Platform signals stabilize after the algorithm indexes your content." |
| Archive (module enabled, no assets) | "No archived assets yet. Asset archives are created after your first completed episode run." |
| Archive reuse queue (no suggestions) | "No reuse suggestions. The system will propose reusing assets after you have 3 or more completed episodes with comparable topics." |
| Learning recommendations (module enabled, no data) | "No recommendations yet. The learning system generates recommendations after 5 published episodes with at least 7 days of platform analytics data." |
| Learning applied (nothing applied) | "No improvements have been applied yet. Approved recommendations are tracked here." |
| Notifications log (module enabled, no deliveries) | "No notifications sent yet. Notifications will appear here after the first triggered event." |
| Templates list (only default templates exist) | "These are your available templates. Create a custom template to encode your own production patterns. [→ Create template]" |
| Users list (only owner) | "Only you have access to this factory. Invite collaborators or clients to share access. [→ Invite user]" |
| Providers swap history (no swaps) | "No provider or model swaps have been applied. Swaps appear here after the first approved recommendation." |
| Config Inspector (no snapshots) | "No config snapshots yet. Snapshots are captured at the start of each episode run and appear here after your first run." |

---

## 12. Accessibility Standards

Factory OS V1 targets **WCAG 2.1 Level AA**. This is the minimum commercial accessibility standard. These are non-negotiable requirements — not aspirational goals.

### 12.1 Keyboard Navigation

- Every interactive element (buttons, links, form fields, tab controls, dropdowns, modals) must be reachable and operable by keyboard alone
- Tab order follows reading order (left to right, top to bottom) on every page
- Modals and drawers must trap focus while open (keyboard cannot navigate outside the modal until closed)
- Escape key must close modals and drawers
- Wizards must be fully completable by keyboard
- Custom dropdowns must respond to arrow keys for option navigation and Enter for selection
- Global search (`Cmd+K` / `Ctrl+K`) must work from any page

### 12.2 Color Contrast

- Text on background: minimum 4.5:1 contrast ratio (AA)
- Large text (18pt+ or 14pt bold): minimum 3:1 contrast ratio
- Status indicators: meet contrast ratios AND use text/icon alongside color (P9)
- Form fields: visible border on all states (default, focus, error, disabled)
- Focus ring: 3:1 contrast ratio against both the element and its background

The factory accent color (#C49A42 in the Hypogeum instance) must be checked against all backgrounds it appears on. If it fails contrast requirements at a specific usage, either the background or the accent treatment must change. Brand color preference does not override accessibility requirements.

### 12.3 Typography

- Minimum body text size: 14px (1rem) rendered at default browser zoom
- Minimum label text size: 12px (0.875rem)
- Line height: minimum 1.5× font size for body text
- Maximum line length: 80 characters for reading-heavy content (approval panels, empty states, explanatory text)
- No text rendered in all-caps beyond short labels (longer uppercase text reduces readability)

### 12.4 Focus States

Every focusable element must have a visible focus ring that:
- Is 2px minimum width
- Contrasts against the element background at 3:1 minimum
- Appears immediately on keyboard focus (no transition delay)
- Is not removed or weakened in CSS (`outline: none` is prohibited without an equivalent custom focus indicator)

### 12.5 Screen Reader Support

- All images and icons must have `alt` text or `aria-label`
- Icons used as the only indicator of meaning (e.g., a status icon with no adjacent text) must have `aria-label`
- Status changes that happen without a full page reload must be announced via `aria-live` regions (e.g., toast notifications, loading state changes, form validation results)
- Tables must have proper `<th>` headers with scope attributes
- Form fields must be associated with their labels via `id` / `for` attributes
- Modals must have a descriptive `aria-label` or `aria-labelledby`

### 12.6 No Color-Only Status Indicators

No status, state, or classification may be communicated by color alone. Every colored indicator must also communicate its meaning via:
- Text label (primary method)
- Icon (secondary method, used alongside text when space is limited)

The status badge standard (C8) defines this for all entity states. No new status indicator may be added that is color-only.

### 12.7 Reduced Motion

For operators who have enabled "reduce motion" in their operating system:
- All animations (loading spinners, transition effects, the running status badge pulse) are replaced with static alternatives
- Page transitions use no animation
- Toast notification slide-in is removed (toasts appear immediately)
- The running badge shows `◉ running` as static text instead of pulsing

Implementation: CSS `prefers-reduced-motion` media query applied to all animation declarations.

---

## 13. White-Label UX Rules

### 13.1 What Is Brandable in V1

These elements change per factory instance via the Settings → Branding configuration:

| Element | Brandable | How Applied |
|---|---|---|
| Logo | Yes | SVG or PNG uploaded to Settings → Branding. Appears in sidebar header and login screen (future). |
| Product name / title | Yes | `factory_branding.dashboard_title` shown in sidebar, browser tab, and page headers. Replaces "Factory OS" or the factory_name. |
| Accent color | Yes | CSS custom property `--accent-color`. Applied to primary buttons, active nav item, links, focus rings. |
| Support email | Yes | Shown in error states that suggest contacting support. |
| Export file naming | Yes | Exports use `{factory_slug}_` prefix. e.g., `hypogeum_channel_export.json` |
| Login screen (future) | Future | Not V1. |
| Custom domain (future) | Future | Not V1. |

### 13.2 What Is NOT Brandable

These elements remain constant regardless of factory branding. Changing them would compromise safety, usability, or consistency.

| Element | Not Brandable Because |
|---|---|
| Error message wording | Safety: error messages must be technically precise. Brand voice does not apply to "MUAPI episode budget exceeded." |
| Confirmation dialog wording | Safety: confirmation dialogs state precise consequences. They must not be softened or paraphrased. |
| Gate verdict labels | Consistency: BGA Pass / Fail, QA Pass / Fail must be consistent across all factory instances for operators who work across instances. |
| Status badge labels | Consistency and accessibility: standardized labels enable keyboard navigation and screen reader predictability. |
| Accessibility elements | Legal: `aria-label`, focus states, and keyboard behavior are not customizable. |
| Navigation structure | Usability: nav structure is defined by `dashboard-surface-area.md`. Branding does not change what nav items exist. |
| Permission behavior | Security: role-based visibility is a security rule, not a UX preference. |

### 13.3 White-Label Technical Requirement

Every color value in the dashboard CSS must be a CSS custom property. No hardcoded hex values in component styles. Example:

Correct: `background-color: var(--accent-color);`  
Incorrect: `background-color: #C49A42;`

The PES accent color token (`--accent-color`) is the only color that changes per factory instance. All other colors (text, backgrounds, borders, status colors) are fixed in the design system and do not vary by branding.

**This requirement applies to V1.** A white-label color swap in V2 must be achievable by changing CSS custom property values only — not by touching component code.

### 13.4 Client Role White-Label Rules

When a Client-role user logs in, the experience is fully scoped to their channels and the factory's brand. The Client must not see:
- The factory entity name (unless it equals the brand name)
- The word "Factory OS" anywhere in the interface
- Navigation items for other clients' channels
- Module names or internal system names (they see capability labels, not system names)

The Client role uses the same dashboard but with:
- `factory_branding.dashboard_title` as the product name (not the factory entity name)
- Channel-scoped views only
- No access to settings, modules, templates, users, or credentials
- Simplified status language (paused → "on hold", awaiting approval → "in review")

---

## 14. UX Review Process

### 14.1 When a UX Review Is Required

The UX/UI Designer must review — and provide a written conformance report — before any of the following can be approved and activated:

| Trigger | Review Required For |
|---|---|
| New dashboard page | Full information architecture, all component specifications, all flows |
| New operator flow (3+ steps) | Flow completeness, step clarity, back/cancel handling, empty states at each step |
| New dangerous action | Confirmation dialog design, consequence language, confirmation level selection |
| New approval pattern | Approval panel structure, evidence display, gating rules for HIGH/CRITICAL |
| New credential or secret form | Form behavior, value masking, save confirmation, test-after-save flow |
| New role-based screen | What each role sees and doesn't see, role-specific empty states |
| New error state | Error message wording, suggested action, resolution path |
| New component type | Component spec including all states, accessibility notes, density rules |
| Changes to navigation structure | Nav item count, ordering, collapsed/expanded default |

### 14.2 What a Conformance Report Contains

The UX/UI Designer's conformance review produces a typed report with:
- Date and reviewer
- Engineering task ID
- Review verdict: `PASS` | `PASS_WITH_NOTES` | `DOES_NOT_CONFORM`
- For `PASS_WITH_NOTES`: list of notes, each citing a PES rule and describing the concern (not blocking, but recorded)
- For `DOES_NOT_CONFORM`: list of violations, each citing a specific PES rule by section number (e.g., "Violates P6: alert has no suggested action") and the required remediation

A `DOES_NOT_CONFORM` verdict blocks the engineering change from activation until the implementing role resolves the cited violations and the UX/UI Designer re-reviews.

### 14.3 UX Review SLA

- Review request submitted: UX/UI Designer must produce a report within **48 hours**
- If not produced within 48 hours: the Engineering Director is notified; the review may be escalated or the change may proceed with a mandatory post-activation review within 7 days
- Emergency fast-path: for critical bug fixes that affect a dashboard element, a 4-hour emergency review SLA applies. Emergency reviews may be less thorough — a full review is required within 7 days of activation.

---

## 15. UX Debt System

UX debt is the accumulation of suboptimal experience patterns that do not violate PES rules but do degrade the product over time. It is tracked, ranked, and resolved like other engineering debt.

### 15.1 UX Debt Sources

| Source | How It Generates UX Debt |
|---|---|
| Dashboard Director | Flags patterns it detects: nav sprawl, approval backlog age, repeated failed form submissions across operators |
| UX/UI Designer | `PASS_WITH_NOTES` conformance reviews — the notes become UX debt items |
| QA Engineer | Test cases that reveal flows are confusing even when technically correct |
| Operator error patterns | Repeated errors at the same step across multiple operators (requires analytics data to detect — not available in V1 without experience metrics) |
| Abandonment in setup flows | Operators who leave the setup wizard at a specific step without completing (requires experience metrics — §16) |

### 15.2 UX Debt Record Schema

```
UXDebtItem {
  debt_id:          string    // e.g., "ux_debt_012"
  title:            string    // one sentence: the problem
  source:           string    // who detected it
  detected_at:      datetime
  affected_page:    string    // which page
  affected_flow:    string | null   // which flow, if applicable
  pes_rule:         string | null   // which PES principle is at risk (if any)
  severity:         "minor" | "moderate" | "significant"
  operator_impact:  string    // how this affects operators in practice
  proposed_fix:     string    // what should change
  effort_estimate:  string    // "small" | "medium" | "large"
  status:           "open" | "in_progress" | "resolved" | "wont_fix"
  resolved_at:      datetime | null
  resolution_note:  string | null
}
```

### 15.3 UX Debt Ranking

UX debt items are ranked by a combined score:

```
priority_score = severity_weight × operator_impact_weight × frequency
  severity_weight:        minor=1, moderate=2, significant=4
  operator_impact_weight: low=1, medium=2, high=3
  frequency:              number of operators/sessions affected (if known) or 1 if unknown
```

Items with `severity=significant` and `operator_impact=high` are automatically escalated to the Engineering Director's change queue as proposed improvements.

### 15.4 UX Debt Review Cadence

- Dashboard Director surfaces UX debt summary on Home as a health dimension
- UX/UI Designer reviews the UX debt backlog monthly and updates severity ratings
- Items open for >90 days with `severity >= moderate` are escalated regardless of priority score
- `wont_fix` decisions require a written rationale stored in the debt record

---

## 16. Experience Metrics

These metrics define what "good product experience" looks like in measurable terms. They are not vanity metrics — each one connects to a specific PES principle or failure mode.

### 16.1 Setup Metrics

| Metric | Definition | Target |
|---|---|---|
| Setup completion rate | % of factory instances that complete the 7-step onboarding flow | ≥ 85% |
| Setup abandonment step | Which step operators most commonly abandon | Should be Step 7 (completed), not Steps 1–4 |
| Time to first run | Minutes from factory creation to first episode queued | ≤ 30 minutes |
| Credential setup success rate | % of credential add attempts that pass the live test | ≥ 90% (failures indicate unclear instructions) |
| Channel activation rate | % of created channels that reach `active` status within 7 days | ≥ 80% |

### 16.2 Approval Metrics

| Metric | Definition | Target |
|---|---|---|
| Approval response time | Time between approval appearing in queue and operator action | Median ≤ 4 hours |
| Deferred approval rate | % of approvals that are deferred (not approved or rejected immediately) | ≤ 20% |
| Reject rate by type | % of each approval type that is rejected | Baseline measurement in V1 |
| Abandoned approval rate | % of approvals that expire without action | 0% (every approval should have a resolution) |

### 16.3 Alert Metrics

| Metric | Definition | Target |
|---|---|---|
| Alert resolution time | Time between alert appearing and being resolved | CRITICAL: ≤ 24h, WARNING: ≤ 7 days |
| Unresolved critical alert rate | % of CRITICAL alerts not resolved within 48h | ≤ 5% |
| Alert false positive rate | % of alerts that operators dismiss as not actionable | ≤ 10% (high rate indicates alert source issues) |

### 16.4 Error and Confusion Metrics

| Metric | Definition | Target |
|---|---|---|
| Failed form submission rate | % of form submissions that fail validation on first attempt | ≤ 15% |
| Repeated form error rate | % of operators who hit the same validation error >2 times | ≤ 5% (suggests unclear field instructions) |
| Navigation dead ends | Sessions where operator navigates to a page and immediately goes back | Baseline measurement in V1 |
| Help request rate | % of sessions where operator needs external help to complete a task | Qualitative measurement in V1 |

### 16.5 Metric Collection Notes

Collecting these metrics requires a session analytics layer not defined in V1's technical architecture. V1 targets are defined here for when collection is implemented. For V1, the UX/UI Designer and QA Engineer assess these metrics qualitatively through direct observation during testing.

---

## 17. Risks and Mitigations

### Risk 1 — Over-designed dashboard

**Failure mode:** The PES document is comprehensive. Frontend Engineers and UX/UI Designers interpret this as a mandate to implement all 17 sections before any operator can use the dashboard. Build time extends by months. Operators wait. The factory has no dashboard.

**Mitigation:**
- The PES is a governance document, not a build sequence. Implement rules as they become relevant.
- PES rules apply to components when they are built. They do not block building other components.
- The dashboard implementation staging (B5A → B5B → B5C → B5D) is the actual build sequence. PES applies to whatever is built at each stage.
- Minimum viable onboarding (§8) is Stage B5C. Full component system is B5D+.

---

### Risk 2 — Enterprise UX complexity too early

**Failure mode:** The PES reads like an enterprise product design specification for a 10,000-user SaaS platform. A factory running on one operator's Mac mini ends up with a dashboard built for 50 concurrent operators, complete with role-switching, audit logs, and approval workflows that add friction to every action.

**Mitigation:**
- The single-owner mode (P1 operator persona, single-operator local deployment) is the primary V1 use case. All PES rules are evaluated against this persona first.
- Role-based features are implemented only when a second user is added. The single-owner factory never sees user management, never sees role-filtered views.
- Advanced features (approval workflows, model swap governance, module lifecycle) are triggered by configuration. Default configuration is minimal. Complex governance is opt-in.

---

### Risk 3 — Hiding important controls

**Failure mode:** The Advanced nav is collapsed by default. The Config Inspector is in Advanced. An operator debugging a configuration issue cannot find the Config Inspector because they do not know Advanced exists or what it contains.

**Mitigation:**
- Every diagnostic deep link (e.g., "View resolved config →") bypasses the Advanced collapse and navigates directly to the correct Advanced page. Operators do not need to discover Advanced — they are linked there from the context where they need it.
- The Home Pending Actions section links directly to the resolution path for any issue — including advanced pages.
- The global search (§9.1) can find any page by name, including advanced pages. "Config Inspector" typed in global search navigates there directly.

---

### Risk 4 — Too many modals

**Failure mode:** Following the P5 dangerous action rule, every action that could have negative consequences is wrapped in a confirmation modal. The dashboard becomes a constant stream of "Are you sure?" dialogs. Operators learn to dismiss them without reading.

**Mitigation:**
- Modal levels (C12) distinguish between standard (Level 1), acknowledged (Level 2), and typed (Level 3) confirmations. Not every action needs a modal.
- The confirmation level is calibrated to the actual risk and reversibility of the action. Pausing a channel (fully reversible) is Level 1. Archiving a factory (irreversible) is Level 3. Viewing a config snapshot (read-only) has no confirmation.
- The UX/UI Designer reviews every new confirmation dialog to ensure the level is appropriate. Over-confirming is a PES violation (it degrades the operator's trust in the confirmation system).

---

### Risk 5 — Weak accessibility

**Failure mode:** WCAG 2.1 AA is stated as a requirement but never validated. Frontend Engineers implement visible functionality; accessibility attributes are an afterthought. When tested with a screen reader or keyboard-only navigation, the dashboard is unusable.

**Mitigation:**
- QA Engineer's acceptance criteria for every dashboard component includes keyboard navigation and color contrast checks.
- Accessibility is not a separate audit — it is part of every QA result. A component that fails keyboard navigation fails QA.
- The UX/UI Designer's conformance report includes accessibility checklist items for every component review.
- No component with `outline: none` in its CSS may pass QA without a documented equivalent custom focus indicator.

---

### Risk 6 — White-label breaking usability

**Failure mode:** An operator sets a very light accent color (near white) for their factory branding. The accent color is used on buttons and links. The buttons become invisible against white backgrounds. The factory's branding preference makes the dashboard unusable.

**Mitigation:**
- Accent color is validated at Settings → Branding save time against a minimum contrast requirement (4.5:1 against white background for normal text, 3:1 for large text).
- If the entered accent color fails the contrast check, the save is rejected with an inline error: "This color doesn't meet contrast requirements against the dashboard background. Choose a darker shade. Minimum contrast ratio: 4.5:1."
- A contrast preview is shown alongside the color picker (before save): "Your accent color on white background: [preview]. Contrast: 3.2:1. ✗ Does not meet AA requirement."

---

### Risk 7 — UX rules blocking useful engineering

**Failure mode:** A Backend Engineer implements a useful new dashboard indicator. The UX/UI Designer marks it as "does not conform" because the component doesn't follow the Card standard (C1). The indicator is blocked for 3 months while the component spec is formally adopted into the PES. A useful improvement is held hostage by process.

**Mitigation:**
- `PASS_WITH_NOTES` is the valve. If a component mostly conforms but has a minor deviation, the UX/UI Designer uses `PASS_WITH_NOTES` rather than `DOES_NOT_CONFORM`. The component ships; the note becomes UX debt to be resolved in the next iteration.
- The UX/UI Designer is explicitly instructed (§14.2): `DOES_NOT_CONFORM` requires a specific cited PES rule violation. Subjective preference ("I would have designed this differently") is not grounds for a blocking verdict.
- PES additions are encouraged. If a component needs a pattern not in the PES, the UX/UI Designer proposes the addition alongside the conformance review. The component ships per the candidate spec; PES is updated in the next revision cycle.

---

### Risk 8 — Design system becoming too rigid

**Failure mode:** The PES is treated as immutable. As the product evolves, operators need experiences that don't fit existing component standards. The design system prevents natural product evolution. Engineers route around the system.

**Mitigation:**
- The PES is a versioned document, not a constitution. Any role can propose a PES revision. The UX/UI Designer reviews it; if the revision is an improvement, the PES is updated.
- The UX Debt system (§15) is the pressure valve. Debt items can become PES revisions. Patterns that recur frequently become component standards.
- The distinction between "does not conform" (blocks activation) and "PES candidate" (ships as proposed addition) ensures the system stays flexible. New patterns are not blocked — they are documented as candidates until formally adopted.
