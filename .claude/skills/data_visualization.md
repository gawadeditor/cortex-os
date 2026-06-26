---
name: data-visualization
description: "Implement quantitative data display for Factory OS server-rendered dashboard using HTML+CSS only (no JS charting libraries). Use when designing charts, progress bars, sparklines, metric tables, or any numeric visualization. Triggers on: chart, visualization, bar chart, progress bar, sparkline, pct_bar, data display, metric table, number formatting, data chart, CSS chart."
---

# Data Visualization

## CONTEXT

This skill governs how Factory OS presents quantitative data. The constraint is strict: this is a server-rendered dashboard with no JavaScript charting libraries. All visualizations must be achievable with HTML + CSS alone, using text, Unicode characters, and CSS layout. No SVG charts. No Canvas. No external chart dependencies.

This constraint is not a limitation — it is a design principle. Text-first data presentation is faster to load, easier to maintain, and more legible at small sizes than pixel-rendered charts. The goal is maximum information density with minimum visual complexity.

The existing `pct_bar` filter (monospace Unicode block characters) is an example of this approach. This skill extends it into a full data visualization system.

## Responsibilities

- **KPI cards**: Design the KPI card component — the primary vehicle for displaying single-metric summaries. Covers: large number, label, trend indicator, benchmark comparison, status border.

- **Trend presentation**: Design how change over time is communicated without a line chart. Options: numeric delta ("+1.2% vs last"), directional indicator (▲▼), sparkline (Unicode, e.g., `▂▃▅▇`). Specify which to use per metric type.

- **Comparative displays**: Design how two items are compared (best vs worst, this platform vs that platform). Scoreboard-style layout. No bar charts — use numeric difference and status coloring.

- **Performance summaries**: Design the performance summary row — a compact, scannable representation of an episode's cross-platform performance in a table row format.

- **Visual information density**: Specify how many metrics can appear per card, per row, per section without causing cognitive overload. Provide density guidelines.

- **Text-first visualizations**: Design text-based visualization components: retention bar (extending pct_bar), signal strength indicator, confidence indicator, coverage indicator.

## Procedure

### Step 1 — KPI card component design

The KPI card is the most-used component in the analytics experience. Specify:

```
┌─────────────────────────┐
│ 4.8%                    │  ← Tier 1: 22px bold, threshold-colored
│ Avg CTR                 │  ← Label: 11px uppercase muted
│ ▲ +0.6% vs last        │  ← Trend: 12px, green if positive
│ ● Above benchmark       │  ← Status: 11px, colored dot
└─────────────────────────┘
```

Specify:
- Exact font sizes for each line
- When trend line appears (only if historical data exists)
- When benchmark line appears (only if benchmark defined)
- Left border color (red/yellow/green/none) as primary status signal
- Minimum/maximum card width
- Grid behavior in the KPI strip

### Step 2 — Text-based visualization inventory

Define the full set of text-based visualizations for this project:

**Retention bar** (`pct_bar` extended):
```
Current: ████████░░ 82%   (using ▓ for filled, ░ for empty)
```
- 10 characters = 100%
- Show percentage label after bar
- Color the filled portion: <30% red, 30–50% yellow, >50% green

**Signal strength indicator**:
```
▲▲▲▲░  (strong positive)
▲▲▲░░  (moderate positive)  
▲░░░░  (weak positive)
▼▼▼▼░  (strong negative)
```
- 5-point scale
- Green triangles for positive, red for negative
- Replace current `"%.2f"|format(ins.signal_strength | abs)`

**Confidence indicator**:
```
●●●●○  (high confidence)
●●●○○  (medium)
●○○○○  (low)
```
- Replace current `pct_bar(10)` for confidence display

**Coverage indicator** (for platform coverage):
```
YT ●  TK ●  IG ○  FB ○
```
- Filled dot = has data, empty dot = no data
- Compact inline format

**Performance dot** (for episode table rows):
```
● ● ● ●   (4 platforms, colored by performance)
```
- Each dot = one platform
- Color = status for that platform's primary KPI

### Step 3 — Comparative display specification

The "Best vs Worst" comparison card must:
- Show side by side (50/50 grid or stacked on mobile)
- Green border-left for best, red border-left for worst
- Primary metric front and center (large text)
- Episode ID as link
- Title/topic in muted text below
- 2 secondary metrics in small text

Specify the HTML structure and CSS classes.

### Step 4 — Episode table row design

Current: 8 columns including ID, Platforms, CTR, Retention (bar), Views, Insights, Action
Proposed: reduce to 6 visible, hide 2 in detail view

New column order and format:
1. **Episode** — ID + title (stacked), 2 lines max
2. **Platforms** — performance dots (1 dot per platform, colored)
3. **CTR** — percentage, threshold colored
4. **Retention** — percentage only (not full bar; bar is secondary)
5. **Views** — integer with commas, right-aligned
6. **Actions** — pending action count badge (if any) + insight count
7. **→** — detail link

Remove: "Recent imports" table on overview page. Move to import history at `/analytics/import`.

### Step 5 — Signal/insight display format

Current insight display: dim-badge + platform text + raw signal_strength float + insight_type text

Proposed:

```
[THUMBNAIL]  [▲▲▲▲░]  CTR exceeded benchmark — YouTube · ep_005
             Strong positive  4.8% CTR vs 3.1% benchmark on YouTube...
```

More specifically:
- Dimension badge (colored, not grey)
- Signal strength indicator (Step 2 format)  
- Human-readable insight headline (first 80 chars of detail)
- Platform + episode in muted text
- Expandable: full detail text on click/toggle

### Step 6 — Empty state visualization

For platform tiles with no data:
```
┌──────────────────────┐
│  YouTube             │
│                      │
│  ○ No data yet       │
│                      │
│  [Import →]          │
└──────────────────────┘
```
- Muted empty circle indicator
- Single CTA button
- No mock numbers

## Deliverables

1. **KPI card specification** — annotated ASCII mockup with exact CSS values
2. **Text visualization library** — all components with Unicode characters and CSS rules
3. **Comparative display spec** — best/worst card HTML structure and classes
4. **Episode table redesign** — column specification, removal rationale
5. **Insight display format spec** — component design with field mapping
6. **Empty state specification** — all cases with visual treatment

## Review Checklist

- [ ] No JavaScript charting libraries used or referenced
- [ ] All visualizations work with HTML/CSS only
- [ ] pct_bar is extended, not replaced (backward compatible)
- [ ] Signal strength communicates urgency, not just magnitude
- [ ] Confidence level is legible at 11px
- [ ] Platform coverage dots have sufficient contrast (not just color)
- [ ] KPI card trend line is conditional (only appears when data exists)
- [ ] Empty states use consistent visual treatment across all pages
- [ ] Episode table column count is ≤7
- [ ] All number formats match the analytics_experience_design metric format table

## Suggested Skills

- `analytics_experience_design` — metric format and threshold rules
- `frontend_polish` — typography scale and spacing for visualization components
- `dashboard_ux` — layout context for where visualizations appear
