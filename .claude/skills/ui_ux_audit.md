---
name: ui-ux-audit
description: "Perform structured usability audit of Factory OS dashboard interfaces using Nielsen Norman heuristics. Produces scored findings (Critical/Major/Minor) with specific fix recommendations. Blocking for Critical findings. Use before implementing any dashboard redesign and after drafting to validate before coding. Triggers on: UX audit, UI audit, usability review, dashboard audit, heuristic evaluation, usability check, interface review, audit dashboard."
---

# UI/UX Audit

## CONTEXT

This skill performs a structured pre-release audit of Factory OS dashboard interfaces. It is not a subjective design opinion. It applies established usability heuristics (Nielsen Norman Group), dashboard-specific effectiveness criteria, and the Factory OS UI conventions defined in `_base_style.html`.

The audit produces a scored report. Each finding has a severity (Critical / Major / Minor) and a specific fix recommendation. The audit is blocking for Critical findings. Major findings require a decision. Minor findings are at operator discretion.

Run this skill BEFORE implementing any dashboard redesign to understand the baseline, and AFTER drafting a redesign to validate it before coding begins.

## Responsibilities

- **Heuristic evaluation**: Apply Nielsen's 10 usability heuristics to each page. Note violations.

- **Usability review**: Identify where the operator must work harder than necessary to get information. Cognitive load, click depth, and scroll distance are measurable.

- **Cognitive load analysis**: Count the number of distinct visual elements on each page. Identify which elements compete for the same attention. Surface the single most impactful simplification per page.

- **Navigation friction analysis**: Walk through each operator flow (e.g., "operator notices poor CTR → investigates episode → takes action"). Count clicks and scrolls. Flag unnecessary steps.

- **Visual hierarchy validation**: Confirm that the most important information has the most visual weight. Check font size, color saturation, and position. If the most urgent information is not the most visually prominent, that is a hierarchy failure.

- **Dashboard effectiveness scoring**: Score each page on the five dimensions below. Produce a summary score per page.

## Procedure

### Pass 1 — Read the template

Read the template file under review. Identify:
- All top-level sections (headers, card groups, tables)
- All KPI/metric displays
- All action elements (buttons, forms, links)
- All navigation elements

### Pass 2 — Heuristic evaluation

Apply these 10 heuristics. Note each violation with page location and severity:

1. **Visibility of system status** — Does the page tell the operator what state the system is in?
2. **Match between system and real world** — Does the language match what production operators say?
3. **User control and freedom** — Can the operator undo actions? Are there escape routes?
4. **Consistency and standards** — Are similar elements styled consistently across pages?
5. **Error prevention** — Does the UI prevent destructive actions?
6. **Recognition over recall** — Does the operator have to remember context from a previous page?
7. **Flexibility and efficiency** — Are there shortcuts for power users?
8. **Aesthetic and minimalist design** — Is every visible element earning its place?
9. **Help users recognize, diagnose, recover from errors** — Are error states clear?
10. **Help and documentation** — Is help available contextually (not in a separate manual)?

### Pass 3 — Cognitive load count

For the page under review:
- Count distinct visual sections
- Count distinct color categories in use
- Count table columns
- Count navigation items visible at once
- Count buttons/CTAs visible at once

Flag: any count that exceeds established thresholds (>5 sections above the fold, >7 table columns, >4 nav items at secondary level, >3 CTAs visible simultaneously).

### Pass 4 — Navigation friction mapping

Walk through these flows for each page:
- **Empty state flow**: What does an operator do when there's no data?
- **Good news flow**: What does an operator do when a metric is green?
- **Bad news flow**: What does an operator do when a metric is red?
- **Action flow**: How does an operator act on a pending recommendation?

Count steps in each flow. Flag flows requiring >3 steps to complete a natural task.

### Pass 5 — Visual hierarchy validation

For each page, rank the visual elements by their measured visual weight (font size, font weight, color saturation, position). Then rank them by operational importance (what an operator SHOULD look at first).

If the two rankings don't match, that is a hierarchy failure. Record the mismatch.

### Pass 6 — Dashboard effectiveness scoring

Score each page on 1–5 scale for these dimensions:

| Dimension | 1 (Poor) | 5 (Excellent) |
|-----------|----------|---------------|
| **Clarity** | Operator must interpret to understand | Meaning is immediate |
| **Hierarchy** | Equal visual weight everywhere | Clear priority order |
| **Density** | Either too sparse or overcrowded | Optimal for decision speed |
| **Action proximity** | Actions are separated from data | Actions are adjacent to data |
| **Completeness** | Key questions not answered | All key questions answered |

## Deliverables

For each page audited:

1. **Heuristic violation list** — Each violation: heuristic ID, location, severity, recommended fix
2. **Cognitive load summary** — Counts vs thresholds, flagged items
3. **Navigation friction map** — Flows with step counts, friction points
4. **Hierarchy mismatch report** — Elements where importance ≠ visual weight
5. **Effectiveness scores** — 5-dimension scorecard per page
6. **Priority fix list** — Top 5 changes per page ranked by impact

## Severity Definitions

- **Critical**: Prevents the operator from completing a primary task. Must be fixed before release.
- **Major**: Creates significant friction or confusion. Should be fixed before release.
- **Minor**: Suboptimal but workable. Fix at discretion.

## Review Checklist

After producing the audit report:

- [ ] Every Critical finding has a specific fix recommendation (not just "make it better")
- [ ] Findings reference the specific template file and line number where applicable
- [ ] Scores are consistent with the evidence (a page cannot score 4/5 on hierarchy while having hierarchy failure findings)
- [ ] Navigation friction maps cover all 4 standard flows per page
- [ ] Cognitive load counts are based on actual template inspection, not guesses

## Suggested Skills

- `dashboard_ux` — use findings to drive the redesign
- `information_architecture` — re-run after redesign to validate IA improvements
- `analytics_experience_design` — for KPI-specific audit findings
