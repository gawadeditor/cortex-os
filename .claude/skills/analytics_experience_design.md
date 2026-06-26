---
name: analytics-experience-design
description: "Design or critique how Factory OS dashboard presents KPIs, analytics, and learning signals. Use when designing analytics pages, structuring metric display, choosing what to surface first, or reviewing analytics UX. Triggers on: analytics dashboard, KPI layout, metric presentation, performance display, analytics UX, learning signals, dashboard analytics section, analytics experience."
---

# Analytics Experience Design

## CONTEXT

This skill handles the specific design challenge of presenting performance analytics in Factory OS. General dashboard UX principles apply, but this skill goes deeper on the analytics-specific problems: how to present KPIs, how to surface what matters most, how to make learning signals actionable.

Factory OS analytics data includes: views, impressions, CTR, retention (avg_view_pct), watch time, likes, comments, shares, saves. These come from YouTube, TikTok, Instagram, and Facebook. Not all metrics are available for all platforms. Not all episodes have data on all platforms. Comparisons must be cross-platform but account for platform-specific baselines.

The analytics experience must serve two audiences simultaneously:
1. **Operational operators** who need to know: "what happened, and what do I do about it?"
2. **Learning system inputs** — the same data feeds the autonomous learning loop; the UI must make this connection visible

## Responsibilities

- **KPI presentation**: Determine the correct display format for each metric. CTR as percentage. Retention as percentage with benchmark indicator. Views with commas. Watch time in seconds (short content) or minutes (long content). Never display raw floats without formatting.

- **Metric prioritization**: Not all metrics matter equally. For short-form content (TikTok, Reels), retention and loop rate matter more than CTR. For YouTube, CTR and retention are co-equal primary signals. Design the KPI hierarchy to reflect platform norms.

- **Performance comparison**: When comparing episodes to each other, or platforms to each other, ensure the comparison is fair. Views are not comparable across platforms. CTR benchmarks differ by platform. The design must make these limitations visible without burying the data.

- **Insight presentation**: AI-generated insights from the analytics store have a `signal_strength`, `affected_dim`, `insight_type`, and `detail`. The design must present these in a way that communicates urgency (signal strength) and specificity (dimension + type) without requiring the operator to decode abbreviations.

- **Recommendation surfacing**: Learning recommendations from the evolution engine must be visible and actionable from the analytics experience. An operator should not need to navigate to `/learning` to see that an action is pending.

- **Learning signal visibility**: Make the analytics → learning pipeline visible. When data is imported, the operator should see that insights were extracted. When an insight becomes an action, the operator should see the connection. The system should not feel like a black box.

## Procedure

### Step 1 — Metric format specification

For each metric in the analytics data model, specify:
- Display format (e.g., "4.8%" not "0.048")
- Display precision (e.g., CTR: 1 decimal place; views: comma-separated integer; retention: 1 decimal place)
- Threshold-based color rules (green/neutral/red)
- Platform-specific benchmark reference (if applicable)

Produce a metric format table covering all fields in `AnalyticsSnapshot`.

### Step 2 — KPI hierarchy by platform

Produce a KPI priority ranking for each platform:

**YouTube**: CTR (P1) → Retention (P1) → Views (P2) → Watch time (P2) → Likes (P3)
**TikTok**: Retention (P1) → Views (P1) → Shares (P2) → Saves (P2) → Likes (P3)
**Instagram**: Views (P1) → Retention (P1) → Saves (P2) → Shares (P2) → Likes (P3)
**Facebook**: Views (P1) → Watch time (P1) → Shares (P2) → Likes (P3)

P1 = Tier 1 KPI (large display), P2 = Tier 2 (medium), P3 = Tier 3 (table only)

### Step 3 — Cross-platform comparison design

Design the cross-platform comparison view for the episode detail page. Key decisions:
- What metrics are shown in a unified cross-platform view? (Only metrics that exist on ≥2 platforms)
- How are platform-specific metrics handled? (Platform-only section below unified view)
- How is "no data" distinguished from "zero value"? (Must be visually distinct)

Produce the cross-platform display specification.

### Step 4 — Insight display design

Design the insight card component. For each insight record:
- `insight_type` → human-readable label (replace underscores, title-case)
- `affected_dim` → colored dimension badge with legend
- `signal_strength` → visual strength indicator (not raw number)
- `detail` → display in full (not truncated); it is the value
- Positive vs negative signal → color coding (green/red) with directional indicator

Produce the insight card component specification.

### Step 5 — Recommendation surface integration

Design the analytics-side recommendation display. Recommendations from `/learning` must appear in two places:
1. **Analytics overview** — a compact "Pending actions" count with direct link
2. **Episode detail** — the specific actions sourced from that episode

Specify the component and its placement on each page.

### Step 6 — Empty state analytics guidance

For the case where analytics have been imported but no insights exist yet:
- What does the operator see?
- What action is suggested?

For the case where analytics haven't been imported yet:
- Specific empty state for each platform tile
- Specific CTA to import

## Deliverables

1. **Metric format table** — all fields, display format, precision, thresholds
2. **KPI hierarchy tables** — per platform, with tier assignments
3. **Cross-platform comparison spec** — field selection and layout
4. **Insight card component spec** — all fields, display format, color rules
5. **Recommendation surface spec** — placement, component design
6. **Empty state matrix** — all empty cases with specific messages and CTAs

## Review Checklist

- [ ] No raw floats displayed (all numbers formatted)
- [ ] CTR < 2% is red everywhere it appears
- [ ] CTR > 5% is green everywhere it appears
- [ ] Retention displayed as percentage with 1 decimal, not as pct_bar only
- [ ] `insight_type` never displayed with underscores
- [ ] `affected_dim` always has a colored badge
- [ ] Positive and negative insights are visually distinct (not just text)
- [ ] Empty states explain WHY there's no data, not just that there isn't any
- [ ] Pending actions are visible without navigating to /learning
- [ ] Platform-specific metrics don't appear in unified cross-platform comparison

## Suggested Skills

- `data_visualization` — for specific visualization format decisions
- `dashboard_ux` — for layout decisions around analytics content
- `ui_ux_audit` — to validate the analytics experience post-design
