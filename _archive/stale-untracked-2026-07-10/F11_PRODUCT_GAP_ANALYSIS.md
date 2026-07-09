# F11 Product Gap Analysis
## Factory OS — Dashboard Product Audit

**Date:** 2026-06-19  
**Scope:** Full audit of current product experience and target product shape  
**Method:** Screen-by-screen audit of all 12 operator-facing routes, data layer review, and product definition for F11 direction  
**Rule:** No schema changes. No migrations. No code. No template modifications.

---

## 1. Executive Summary

### Why the current product is still developer-facing

Factory OS works. The pipeline runs, the gates fire, the costs are tracked, the analytics are imported. The infrastructure is real. But the product experience was designed for the person who built it — someone who knows what OIS means, what a gate verdict is, what "Decision Inputs" refers to, and why "Preflight" lives in the nav.

That person is an engineer. The dashboard should serve a media operator.

A media operator opens their dashboard to answer five questions:

1. Is my channel healthy?
2. What just published and how is it performing?
3. Am I hitting my targets?
4. What should I make next?
5. Is anything broken that I need to fix?

The current dashboard cannot answer any of these questions cleanly. Instead it offers:

- A nav bar with 14 items, most named after internal system concepts
- A "Production Overview" card that shows `ep_003` as the current episode (not the title)
- Analytics KPIs where 3 of 4 are always `—` until the operator manually imports CSV data
- A "Connected Platforms" grid that shows connection state but no actual platform performance
- No thumbnails displayed anywhere in the product
- No revenue surface of any kind
- No title performance analysis
- No learning insights on the main dashboard — they are hidden behind the "Intelligence" nav item
- An "Operations" section collapsed by default containing links with names like "Active OIS Signals" and "Data Readiness"

The core problem is structural: the dashboard was built incrementally, with each phase adding its own section. The result is a page that represents the architecture of the system rather than the mental model of the operator who uses it.

### What must change before more schema work

Nothing in the schema needs to grow until the product surface that would consume new schema fields is designed and built. Adding `retention_curve` or `campaign_id` to the database before there is a page that displays them is schema speculation, not product work.

F11 must stop expanding schema and start building product. The sequence is:

1. Define the product shape — what the operator sees and what decisions each screen enables.
2. Identify the schema gaps that block the product, not the schema gaps that might someday be useful.
3. Build the product surfaces in the correct operator-mental-model order.
4. Run the factory. Discover what's actually missing.
5. Then and only then, expand schema to support what was learned from running.

---

## 2. Current Roadmap Correction

The correct sequence for this project is not "build all the intelligence layers first, then ship." That path produces a complete backend and an unusable frontend.

The corrected roadmap:

**Phase 1 — Complete the Factory OS dashboard**  
Build the real operator dashboard as defined in this document. Channel identity, connected platforms, published videos with thumbnails and titles, production status, views, revenue, costs, warnings, and learning insights — all on one coherent page that answers the five operator questions above.

**Phase 2 — Create the first video from inside the dashboard**  
The first end-to-end production run must be launchable entirely from the dashboard UI. No command line. No `.env` editing. No manual DB entries. If an operator cannot launch a run, monitor it, and see the result — including the published video with its thumbnail — without touching a terminal, the product is not ready.

**Phase 3 — Run the factory automatically for one month**  
Activate the scheduler. Let the factory produce episodes without manual intervention for 30 days. Monitor through the dashboard only. Collect real performance data from YouTube.

**Phase 4 — Fix production issues during that month**  
The issues that surface during a month of real automated operation are the issues that matter. Not hypothetical schema gaps. Real failures: wrong thumbnails, broken titles, cost overruns, credential expiries, publishing failures. Fix these through the dashboard, not the terminal.

**Phase 5 — Move the stable factory to the Mac mini**  
Once the factory has run for a month without the laptop, migrate to the always-on machine. The dashboard must be accessible from any device. The factory must survive restarts.

**Phase 6 — Let it continue running**  
The factory becomes a background process. The dashboard is the only interface. The operator checks in, reviews performance, approves learning actions, and adjusts publishing targets. No engineering involvement for routine operation.

**Phase 7 — Build V2 for broader media-company use cases**  
Only after the single-channel factory is stable and self-running does V2 begin. V2 is not a YouTube-only product. It is a multi-channel, multi-platform, multi-client media factory with access control, white-labeling, and SaaS deployment. V2 builds on the learnings from six months of real operation, not on speculative schema design.

---

## 3. Current Screens Audit

### /dashboard

**What the user currently sees:**  
Eight sections in order: (1) Channel Identity — name, niche tag, status, readiness score pill, channel list. (2) Connected Platforms — four cards showing YouTube/TikTok/Instagram/Facebook with connection state and analytics record count. (3) Production Overview — current episode ID, current stage, progress bar, queue size, run health %, total run count. (4) Recent Episodes — table with columns: Episode, Date, Status, Views, CTR, Retention, Platform ID. (5) Analytics Summary — four KPIs: Subscribers, Total Views, Watch Time, Production Costs. (6) Charts — views trend bar chart and weekly targets bars. (7) System Alerts — colored dots with labels and Resolve buttons. (8) Operations — collapsed `<details>` containing eight links.

**What decision the screen helps with:**  
Currently: none clearly. It is a status board, not a decision surface. The operator cannot take any meaningful action from it except click through to other pages.

**What is confusing:**  
- "ep_003" as the current episode name. Operators don't think in episode IDs.  
- "Run Health 92%" — a statistic computed over all-time runs. Meaningless as an immediate status signal.  
- "Readiness score 87%" in a pill with no breakdown and no call to action.  
- Three of four Analytics KPIs showing `—` by default because analytics must be manually imported.  
- "Platform ID" column in the episodes table — raw video ID string, not a labeled link.  
- Operations section collapsed by default. Hidden navigation is not navigation.  
- TikTok, Instagram, Facebook cards always showing "Not connected / Demo data only" — permanently empty cards occupying 75% of the grid.

**What should be hidden under Operations/Advanced:**  
- The readiness score pill (it's an internal audit metric, not an operator signal)  
- "Total Runs" counter  
- "Run Health" percentage  
- The collapsed Operations section should move its content to a separate /operations or /system page

**What should be promoted to the main dashboard:**  
- Thumbnail of the last published video  
- Published title (not episode ID)  
- Views and CTR for the last published video with a clear trend indicator  
- Subscriber count prominently displayed  
- Revenue panel (even if empty, the placeholder makes clear the factory tracks it)  
- Days since last publish with cadence health ("On track" or "6 days overdue")  
- One clear learning insight if available

---

### /command

**What the user currently sees:**  
Factory status banner (HEALTHY / DEGRADED / BLOCKED) with "Director score: 87/100" and a timestamp. Below: Needs Attention queue with decision cards showing evidence in monospace boxes. Next Decision card. Launch Readiness block. Action Plans (if any). Active Production — four panels: Running Now, Queued, Last Completed, Recent Failures. Provider Connectivity (collapsed). Decision Data (collapsed). Investigate — a row of buttons linking to every other page.

**What decision the screen helps with:**  
This is the most useful page in the product for an operator who understands the system. "Can I launch? What's broken? What needs my review?" The Launch Readiness block and the Needs Attention queue are genuinely valuable.

**What is confusing:**  
- "Director score: 87/100" — what is a director score? An operator does not know this is the Dashboard Director's health audit.  
- "OIS signals" button in the factory banner — OIS is never explained in the UI.  
- Evidence blocks in monospace — internal log formatting, not operator-readable messages.  
- "Preflight" button — this is an engineering term.  
- "Decision data" collapsed section showing "Decision Engine Readiness" — the operator does not interact with a decision engine directly.  
- "Investigate" section at the bottom is a link farm with 10 buttons including "Failures", "OIS signals", and "Action plans."

**What should be hidden under Operations/Advanced:**  
- Director score  
- OIS signals link  
- Decision Data section  
- Provider Connectivity section (should only surface when there is an actual problem)  
- Investigate link farm

**What should be promoted to the main dashboard:**  
- Launch Readiness block — this is the best single UX element in the product and belongs on the main dashboard, not hidden behind a /command nav item  
- Needs Attention queue — with operator-language labels, not internal event names  
- Active Production panels (Running Now / Queued)

---

### /analytics

**What the user currently sees:**  
Sub-navigation: Portfolio | YouTube | TikTok | Instagram | Facebook | Import. Portfolio view shows episode-level performance across all platforms. Per-platform views show breakdowns by episode. Each episode row shows views, CTR, retention, watch time, and a link to the episode detail page.

**What decision the screen helps with:**  
"Which of my episodes performed best? What is my channel-level trend?" This is the right surface for performance review, but it requires the operator to navigate here and remember to check it.

**What is confusing:**  
- No thumbnails in the episode list — the operator cannot visually identify which video they're looking at without reading the episode ID.  
- Platform tabs for TikTok/Instagram/Facebook are empty for any operator who hasn't connected those platforms. Empty tabs that can't be dismissed create confusion.  
- No comparison view — the operator cannot see two episodes side by side.  
- No revenue column or revenue trend.

**What should be hidden under Operations/Advanced:**  
Nothing — this is a legitimate operator surface.

**What should be promoted to the main dashboard:**  
- The most recent episode's CTR and views (summary card, not full table)  
- One chart: CTR trend over last 12 episodes

---

### /learning

**What the user currently sees:**  
"Intelligence" page. Header shows total insight count and total analytics records. If there are pending actions, a yellow banner shows "N actions need your review." Action rows list each action with dimension badge (thumbnail/title/hook/topic), action type badge, Apply and Dismiss buttons. Below: recent recommendations list with confidence bars. Below: recent learning records list with signal types, values, and episode links.

**What decision the screen helps with:**  
"What has the factory learned? What should I apply to the next episode?" This is a legitimate and important operator surface.

**What is confusing:**  
- "Actions need your review" with no visual context for what the action is proposing. The action shows a dimension badge ("thumbnail") and a rationale sentence, but no before/after comparison, no visual evidence, no CTR numbers that led to the recommendation.  
- "confidence bar" is a blue progress bar next to a decimal (0.73). Operators don't think in confidence scores.  
- Learning records section at the bottom shows raw signal data (signal_type: "weak_thumbnail", signal_value: -0.72) — this is database data, not operator-readable insight.  
- The page is named "Intelligence" in the nav but "Intelligence" in the `<h1>`. The nav link says "Intelligence," the page says "Intelligence." This inconsistency compounds the sense that the product is not intentional.

**What should be hidden under Operations/Advanced:**  
- Raw learning records list — this is audit data, not operator-facing  
- Confidence scores as decimals — replace with language ("high confidence", "low confidence")

**What should be promoted to the main dashboard:**  
- One top learning insight as a card on the main dashboard: "Based on 5 episodes, question-format titles outperform statement titles by 23% CTR. Recommended for next episode."

---

### /decision-inputs

**What the user currently sees:**  
"Decision Inputs" page header with "investigative view" subtitle. Decision Engine Readiness banner (Ready / Partial / Not ready) with five readiness dots: Discovery, Episodes, Costs, Publishing, Audience. Below: a 3-column grid of data cards — Discovery, Episodes, Production Costs, Publishing, Audience Response. Each card shows counts and status.

**What decision the screen helps with:**  
This page answers: "Does the factory have enough data to generate decisions?" That is an internal system question, not an operator question.

**What is confusing:**  
- "Decision Engine Readiness" — operators do not interact with decision engines. They interact with their channel.  
- "Missing: discovery, audience" — discovery of what? Audience of what? This is the internal readiness check vocabulary leaking into the operator layer.  
- "Investigative view" subtitle — this is a self-description that confirms the page was not designed for operators.  
- The entire page could be replaced by a single status indicator on the /command page. It does not warrant a nav item.

**What should be hidden under Operations/Advanced:**  
The entire page. It should be accessible via /operations/decision-data or from a System Health section, never from the top-level navigation.

**What should be promoted to the main dashboard:**  
Nothing directly. If analytics data is missing, that warning should surface in the Analytics section as "No analytics imported yet — import from YouTube Studio."

---

### /decisions

**What the user currently sees:**  
Engine status banner (Ready / Partial / Not ready). Below: a list of decision cards ranked by priority. Each card shows: title, recommendation sentence, evidence block in monospace, confidence bar, priority badge, decision type, and an action button linking to the relevant page.

**What decision the screen helps with:**  
"What should I do next to improve my factory?" This is a legitimate operator question, but framed in engineering terms.

**What is confusing:**  
- Decision cards show evidence in monospace code blocks: `views: 15000\nctr: 0.048\navg_view_pct: 0.52`. An operator cannot use raw metric dumps as evidence.  
- "confidence: 73%" — operators don't know what confidence means in this context.  
- Priority 1 / Priority 2 / Priority 3 badges — an internal ranking system made visible.  
- Decision types like "thumbnail_strategy" and "content_diversification" are internal classifier names, not operator language.

**What should be hidden under Operations/Advanced:**  
- Confidence scores as numbers  
- Evidence monospace blocks  
- Decision type badges  
- Priority number badges

**What should be promoted to the main dashboard:**  
- The single top decision as a card: "Your last 3 thumbnails had below-average CTR. Try a different visual approach. [Review recommendation →]"

---

### /decision-reviews

**What the user currently sees:**  
A queue of decision proposals for the operator to accept or reject. Each card shows: decision title, recommendation sentence, evidence (in monospace), confidence score, and Accept/Reject buttons. A "review queue" count in the sub-header.

**What decision the screen helps with:**  
"Do I agree with this recommendation? Should I act on it?" This is a legitimate and important operator workflow — the human-in-the-loop approval gate.

**What is confusing:**  
- Same evidence-in-monospace problem as /decisions.  
- Accept and Reject are the only affordances. There is no "remind me later" or "I need more context" option.  
- The page has no visual context for the recommendation — no thumbnail preview, no chart, no before/after comparison. The operator is asked to approve a text recommendation without seeing the evidence visually.

**What should be hidden under Operations/Advanced:**  
- Raw evidence monospace blocks  
- Internal decision_id references

**What should be promoted to the main dashboard:**  
- Pending review count as a badge on the "What needs attention" section

---

### /execution-plans

**What the user currently sees:**  
A list of execution plans derived from accepted decision reviews. Each plan shows: decision type, action summary, planned steps, risk level, blockers, and Approve/Reject/Dry Run buttons. A warning: "Dry-run only — no live execution available."

**What decision the screen helps with:**  
"How exactly will this recommendation be applied? Is it safe?" This is the right level of detail for a final approval before changes are made.

**What is confusing:**  
- "Dry-run only — no live execution available" makes this page a dead end. The operator can approve a plan that does nothing. Plans accumulate without effect.  
- Plan status values ("draft", "approved", "rejected", "executed", "cancelled") are shown as badge text without explanations.  
- "Risk level: medium" with no explanation of what makes it medium or how to reduce it.

**What should be hidden under Operations/Advanced:**  
The entire page. Until live execution is wired, this surface confuses operators. Move to /operations/execution-plans.

**What should be promoted to the main dashboard:**  
Nothing until execution is live.

---

### /channel-profile

**What the user currently sees:**  
A form with: Channel Name, Description, Content Niche, Tone & Style, three color pickers (Primary, Secondary, Accent), and a grid of five number inputs for weekly publishing targets per platform.

**What decision the screen helps with:**  
"Who is my channel and what do I publish?" This is foundational configuration — correct surface, correct placement.

**What is confusing:**  
- The color pickers have no visual preview of how the colors are applied anywhere in the dashboard or in generated content. The operator has no feedback that changing "primary color" affects anything.  
- The form lacks: logo upload, subscriber goal, monthly revenue target, monetization status, channel URL, platform account IDs (YouTube channel ID, TikTok @handle).  
- "Tone & Style" is a free-text field with a placeholder "Educational, Conversational, Documentary" — no structure, no validation, no preview of how it affects agent prompts.  
- Publishing targets are raw numbers. The form doesn't tell the operator what the factory currently achieves vs. targets.

**What should be hidden under Operations/Advanced:**  
Nothing — this is operator-facing by nature.

**What should be promoted to the main dashboard:**  
- A compact channel identity summary block at the top of /dashboard: logo + name + subscriber count + cadence health  
- Publishing targets vs. actuals comparison on the main dashboard

---

### /platforms/connect/\<platform\>

**What the user currently sees:**  
A form with a title "Connect [Platform]," instructional text, a yellow warning "This stores the credential locally. No live API calls are made now," a monospace text input for the credential, a label input, and a Save Credential button.

**What decision the screen helps with:**  
"How do I give the factory access to my YouTube account?"

**What is confusing:**  
Everything about this page is wrong for YouTube specifically. YouTube does not use a simple API key for publishing and analytics access. It requires OAuth 2.0 with a client secret, a client ID, a redirect URI, and a browser authorization flow. Pasting an API key into a text box and saving it locally does not connect a YouTube account. The credential stored here is not used for OAuth — it may be used for YouTube Data API read access (which uses an API key), but publishing and full analytics require a different credential type entirely.

The instructions say: "No live API calls are made now. Credentials are tested only when analytics are imported." This means the operator receives no confirmation that their credential actually works until they attempt to import analytics — which may fail silently or with a cryptic error.

There is also no distinction between:
- YouTube Data API key (for reading public data)
- YouTube OAuth credentials (for accessing the operator's own channel data and publishing)
- TikTok Creator API token (different flow, different scopes)
- Meta Graph API token (Instagram + Facebook, requires Meta Business app setup)

All four platforms are presented with the same "paste your API key here" UX, which is incorrect for all of them.

**What should be hidden under Operations/Advanced:**  
The current implementation. It should not be accessible until it is replaced with platform-specific correct flows.

**What should be promoted to the main dashboard:**  
Connection status per platform as part of the Channel Identity block.

---

### /analytics/import

**What the user currently sees:**  
A sub-navigation bar (Portfolio, YouTube, TikTok, Instagram, Facebook, Import). A "How to import" guidance card. A form with: Episode selector (dropdown or custom ID), Platform selector, and three import method tabs: Generate demo data, Paste JSON, Paste CSV. Demo data tab has a yellow warning. JSON tab shows a format hint per platform. Submit button.

**What decision the screen helps with:**  
"How do I get my real YouTube analytics into the factory?"

**What is confusing:**  
- "Generate demo data" is the default selected tab. The first thing the operator sees is an invitation to generate fake data. For an operator who wants real data, this sets the wrong expectation immediately.  
- The JSON format hint shows raw metric keys (e.g., `"avg_view_pct": 0.52`) which the operator must manually map from whatever YouTube Studio exports. YouTube Studio does not export JSON. It exports CSV. The JSON tab is a developer feature, not an operator feature.  
- There is no "If your platform is connected, we can fetch analytics automatically" path. The import is always manual.  
- Episode selection by episode ID requires the operator to know their internal episode IDs. Most operators will not know that the video they published as "The Antikythera Mechanism" is internally `ep_003`.

**What should be hidden under Operations/Advanced:**  
- JSON import tab (keep CSV and auto-fetch)  
- Demo data tab (move to a /developer or /testing section)  
- Episode ID selector (should be replaced by a published video selector showing thumbnails and titles)

**What should be promoted to the main dashboard:**  
- A "Sync Analytics" button per platform on the platform connection card

---

### /published/import

**What the user currently sees:**  
A form with: Episode ID selector, Platform selector, Publishing Status selector (success/failed/pending/skipped/unknown), Platform Video ID text input, Published At datetime input, URL input, Run ID input, Failure Reason input. Save Record button.

**What decision the screen helps with:**  
"How do I tell the factory that I published a video outside of its pipeline?"

**What is confusing:**  
- The form asks for a "Run ID" — this is an internal system concept that has no meaning to a content operator. A video published manually or before the factory existed has no run ID.  
- "Platform Video ID" is technical. The label should be "YouTube Video ID (e.g., dQw4w9WgXcQ)" with a link to where to find it.  
- The form has no thumbnail field, no title field, no description field, no hashtag field. The published video is recorded as a data record with no content context.  
- Publishing Status defaulting to "success" is correct but "unknown" and "skipped" are confusing options in a manual import context.  
- This entire form could be replaced by: paste a YouTube URL → factory parses the video ID, fetches public metadata (title, thumbnail, publish date), and pre-fills the record.

**What should be hidden under Operations/Advanced:**  
- Run ID field  
- Failure Reason field (keep only when status is "failed")  
- Raw Publishing Status dropdown — replace with "Published successfully" checkbox

**What should be promoted to the main dashboard:**  
- A "Register existing video" button on the Recent Episodes section when the episode list is empty or a video exists on YouTube that isn't in the factory

---

## 4. Required Main Dashboard Product Shape

The target /dashboard layout. Everything on one page, in operator priority order.

---

### Row 1 — Channel Identity Bar

Full-width bar at the top. Always visible.

```
[Logo]  The Hypogeum Chronicles          ● YouTube Connected    12,400 subscribers
        Ancient History Documentary      ● TikTok: not connected
                                         ○ Instagram: not connected
```

Fields:
- Channel logo (from channel_profile_settings.logo_path)
- Channel name (channel_profile_settings.channel_name)
- Content niche (human-readable, not slug)
- Per-platform connection pill: connected/not-connected with direct connect link
- Subscriber count from last analytics import (YouTube primary)
- Last updated timestamp for subscriber count

---

### Row 2 — Warnings Bar (conditional, only when warnings exist)

Full-width, yellow border. Shows maximum 3 operator-language warnings.

```
⚠ You haven't published in 9 days · Target: every 3 days
⚠ YouTube credential expires in 5 days · Renew now →
⚠ Last episode CTR 2.1% — 40% below your channel average
```

Rules:
- Only content and channel health warnings. No pipeline internals.
- Maximum 3. If more than 3, show "2 more warnings →" linking to /warnings.
- Each warning has a direct action link.
- Dismissible per warning (stored so dismissed warning doesn't re-appear for 7 days).

---

### Row 3 — Last Published Video

Full-width card. This is the most important content section.

```
┌─────────────┬────────────────────────────────────────────────────────────┐
│ [Thumbnail] │ The Antikythera Mechanism: Ancient Greek Computer          │
│  120×68px   │ Published June 14, 2026 · YouTube · 6 days ago            │
│             │                                                            │
│             │ Views: 4,821   CTR: 5.2%  ↑ above avg   Retention: 62%   │
│             │ Watch time: 182 hours   Revenue: $3.40                    │
│             │                                                            │
│             │ [Watch on YouTube ↗]  [View episode →]  [Import analytics]│
└─────────────┴────────────────────────────────────────────────────────────┘
```

Fields:
- Thumbnail image (from thumbnail_candidates where selected=1, or archive_index)
- Published title (from titles where selected=1, not internal episode topic)
- Platform published to
- Published date in human time ("6 days ago")
- Views with trend indicator vs. channel average
- CTR with performance band label ("above avg / below avg / on baseline")
- Average retention percentage
- Total watch time hours
- Revenue (if available; hidden if not monetized)
- Three action links: platform link, episode detail, import analytics

---

### Row 4 — Production Status

Two-column row. Left: Current production. Right: Queue.

**Left — Current production (or "Idle"):**
```
Currently producing:
"The Oracle at Delphi" — ep_005
Stage: Visual Production (scene 7/26)
Started 18 min ago · $2.40 spent so far
[View live →]
```
When idle: "Ready to produce · No episode currently running"  
Next topic in queue (from episode_topics) shown as the next available.

**Right — Queue:**
```
Queue: 2 episodes waiting
────────────────────────
1. The Roman Forum Discovery
2. Pompeii: The Final Hours

[+ Add topic]  [View queue →]
```

---

### Row 5 — Performance KPIs (5 cards)

Horizontal strip. These are channel-level, last 30 days.

```
┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐
│  12,400   │ │  47,200   │ │   5.4%    │ │  1,840 h  │ │   $28.40  │
│Subscribers│ │  Views    │ │   Avg CTR │ │Watch Time │ │  Revenue  │
│ +340 /mo  │ │  30 days  │ │  ↑ +0.4%  │ │  30 days  │ │  30 days  │
└───────────┘ └───────────┘ └───────────┘ └───────────┘ └───────────┘
```

Show `—` with an "Import analytics to see this" link when no real data exists. Never show mock data. Never show 0 when no data exists.

---

### Row 6 — Publishing Targets vs. Actuals

Two columns. Left: current month progress. Right: weekly cadence health.

**Left:**
```
This month: June 2026
Published:  2 episodes / 4 target  ████░░░░░░  50%
Remaining:  11 days to hit target
Pace:       at current rate, you'll finish 1 episode short
```

**Right:**
```
Last 7 days:
YouTube Videos:   1 / 2  ██░░░░░░░░
YouTube Shorts:   3 / 3  ██████████  ✓
TikTok Posts:     0 / 5  ░░░░░░░░░░  Not connected
Instagram Reels:  0 / 3  ░░░░░░░░░░  Not connected
```

Actuals come from publishing_outcomes joined to created_at. Targets come from channel_profile_settings.

---

### Row 7 — Cost / Revenue / Profit

Three-column card. Production vs. earnings.

```
┌──────────────────┬──────────────────┬──────────────────┐
│  Production Cost │ Platform Revenue │  Net Profit/Loss  │
│     $47.20       │     $28.40       │     -$18.80       │
│   this month     │   this month     │   this month      │
│   $11.80/video   │   $14.20/video   │                   │
│   ↓ -5% vs last  │   —  no prior    │   Break-even at   │
│                  │                  │   4 videos/month  │
└──────────────────┴──────────────────┴──────────────────┘
```

Revenue shows `—` if not monetized. "Break-even" line is computed from cost per video and revenue per video.

---

### Row 8 — Charts (3 charts side by side)

**Chart 1 — Views trend (last 12 episodes)**  
Bar chart. Blue bars = views. One bar per episode, ordered oldest to newest.  
Tooltip shows episode title and view count on hover.

**Chart 2 — CTR trend (last 12 episodes)**  
Line chart or bar chart. Shows CTR per episode with channel average as a horizontal reference line. Episodes above average shown in green; below in red.

**Chart 3 — Revenue vs. Cost (last 12 episodes)**  
Stacked or grouped bar chart. Blue = production cost. Green = revenue. The gap is the loss (or gain). Makes break-even point visible across episode history.

---

### Row 9 — Recent Episodes (with thumbnails)

Table with thumbnail column. Last 8 episodes. Scrollable.

```
[Thumb]  Title                          Platform   Views    CTR    Revenue   Status
[img]    The Antikythera Mechanism...   YouTube    4,821    5.2%   $3.40     ● Published
[img]    The Oracle at Delphi           —          —        —      —         ✗ Failed
[img]    The Roman Forum                YouTube    3,140    4.1%   $2.10     ● Published
```

- Thumbnail column: 80×45px thumbnail image. Clicking opens episode detail.
- Title column: published title, not episode ID.
- Performance band indicator next to CTR: ↑ above avg / → on baseline / ↓ below avg.
- Revenue shown or `—` if not monetized.
- Published status: "Published 6 days ago" or "Failed · [Retry]" or "In production."

---

### Row 10 — Top Learning Insight (conditional)

Single card, shown only if intelligence engine has a high-confidence insight.

```
💡 Learning Insight
"Your last 6 episodes show that question-format titles (5 episodes)
outperform statement titles (2 episodes) by 31% average CTR.
The next episode topic is a strong candidate for a question format."
[Apply to next episode →]  [View all insights →]
```

Only one insight shown. Highest confidence available. If no insights exist (not enough data), the card is absent — not replaced with an empty state.

---

### Collapsible: Operations

At the bottom, collapsed by default. Contains links to all developer/system pages: Run History, Gate Verdicts, OIS Signals, Decision Inputs, Preflight, Action Plans. These are legitimate needs for the engineer but not for the operator's daily workflow.

---

## 5. Episode Object Definition

The product-level Episode is a content unit, not a pipeline run record. A single Episode may have multiple production runs (retries), but only one Episode represents the canonical video.

```
Episode
│
├── Identity
│   ├── episode_id            internal stable identifier
│   ├── title                 published title (the one that went live)
│   ├── topic                 topic brief given to the pipeline
│   ├── description           YouTube/platform description text
│   └── hashtags              generated from episode content + channel brand
│
├── Content Artifacts
│   ├── script                full narration script (text)
│   ├── voiceover             final audio file path
│   ├── generated_assets      list of video clips, images produced
│   └── final_video           path to composed final video file
│
├── Publishing
│   ├── upload_status         per platform: not_uploaded | uploading | published | failed
│   ├── platform_video_id     per platform: the platform's own ID for the video
│   ├── url                   per platform: direct watch URL
│   └── published_at          per platform: publish timestamp
│
├── Thumbnails
│   ├── candidates            list of 3+ thumbnail images
│   │   ├── image_path        file path to the thumbnail image
│   │   ├── prompt            the text prompt used to generate this thumbnail
│   │   ├── model             which image model generated it
│   │   ├── seed              generation seed if available (for reproducibility)
│   │   └── cost              generation cost for this candidate
│   ├── selected              which candidate was published
│   └── performance           post-publish CTR, impressions, views for selected thumbnail
│
├── Titles
│   ├── candidates            list of generated title options
│   │   ├── title_text        the title string
│   │   └── title_type        format: question / list / statement / mystery / how_to
│   ├── selected              which title was published
│   └── performance           post-publish CTR, impressions for selected title
│
├── Branding
│   ├── logo_bug              watermark/logo overlay applied to video
│   ├── hashtag_overlay       per-episode dynamic hashtag overlaid on video or description
│   └── overlay_position      position template used for logo and text overlays
│
├── Schedule
│   └── publishing_schedule   per platform: scheduled publish datetime
│
├── Economics
│   ├── production_cost       total AI and compute cost for this episode
│   ├── platform_revenue      revenue earned per platform (from analytics)
│   ├── sponsor_revenue       manual revenue from sponsorship if applicable
│   └── net_profit_loss       revenue minus production_cost
│
├── Analytics
│   └── per_platform          for each published platform:
│       ├── views
│       ├── impressions
│       ├── ctr
│       ├── avg_view_pct      average percentage of video watched
│       ├── watch_time_hours
│       ├── likes
│       ├── comments
│       ├── shares
│       ├── saves
│       └── revenue_usd
│
└── Learning Notes
    └── notes                 what the intelligence engine learned from this episode
                              (highest-signal insight, plain language)
```

The Episode object definition drives both the episode detail page design and the schema fields actually required. Schema follows the product object — not the other way around.

---

## 6. Thumbnail Intelligence Product Requirement

Every episode must display its thumbnail candidates as a visual comparison surface. The operator must be able to answer: "Which thumbnail worked? Why? What should I try next?"

**Per episode — Thumbnail Comparison view:**

Three (minimum) thumbnail candidate cards displayed side by side or stacked.

Each candidate card shows:
- Thumbnail image preview (full visual — not a file path string)
- Prompt used to generate this thumbnail (collapsible — show first 100 chars, expand on click)
- Model/seed used for generation
- Cost to generate this candidate
- "Published" badge if this was the selected thumbnail

For the published (selected) thumbnail only:
- CTR — click-through rate (e.g., 5.2%)
- Impressions — how many times it was shown
- Views — how many clicks it received
- Retention impact — whether videos with this thumbnail style show different retention than channel average
- Performance label: "Winner" / "On baseline" / "Below average" compared to channel CTR average

At the bottom of the comparison:
- Recommendation for next episode: "Your last 3 published thumbnails with face + high contrast text show 28% higher CTR than faceless thumbnails. Recommended style for next episode: [description of winning pattern]."

For candidates that were generated but not published:
- Show the image and prompt
- Note: "Not published — no performance data"
- But include in the visual comparison so the operator can see what options existed

**Thumbnail gallery on /dashboard:**  
A horizontal strip of the last 8 published thumbnails with their CTR overlaid. Clicking any thumbnail navigates to the episode detail page. This makes the channel's visual identity immediately visible and lets the operator scan for patterns without opening individual episodes.

---

## 7. Title Intelligence Product Requirement

Every episode must display its title candidates with performance data for the published title. The operator must be able to answer: "Which title format drives the most clicks? What should I try next?"

**Per episode — Title Comparison view:**

A card listing all generated title candidates.

Each candidate shows:
- Title text (full string)
- Title format classification: Question / List / Statement / Mystery / How-To / Number Lead
- Character count
- Word count
- "Published" badge if this was the selected title

For the published (selected) title only:
- CTR — click-through rate
- Impressions
- Views resulting from this title
- Retention impact (does high CTR title predict better or worse retention?)
- Performance label: "Above average" / "On baseline" / "Below average" vs. channel CTR

Candidates not selected:
- Show title text and format
- Note: "Not published — no performance data"
- These are still valuable for the intelligence engine to learn what formats exist in the operator's vocabulary

**Cross-episode title pattern summary:**  
Below the per-episode view, a section showing:
- Question titles: 5 episodes, avg CTR 6.1%
- Statement titles: 3 episodes, avg CTR 4.2%
- Number Lead titles: 2 episodes, avg CTR 5.8%
- Recommended direction: "Question format is your strongest performing title type."

**Recommendation for next episode:**  
One sentence: "Based on 10 published episodes, question-format titles with 'hidden' or 'secret' in the text outperform your channel average by 34% CTR. Suggested direction for next episode."

---

## 8. Cross-Platform Performance Requirement

When an episode is published to multiple platforms, the dashboard must show the performance of that same content across platforms in a side-by-side comparison.

**Per episode — Cross-Platform view:**

A table or card grid, one column per connected platform:

```
                    YouTube         TikTok          Instagram       Facebook
Views               4,821           12,400          2,100           —
CTR                 5.2%            N/A             N/A             —
Avg Retention       62%             34%             45%             —
Watch Time          182 h           8.1 h           2.4 h           —
Revenue             $3.40           —               —               —
Published           Jun 14          Jun 14          Jun 15          Not published
Audience Response   ↑ above avg     ↑ above avg     → on baseline   —
```

Cells show `—` for platforms not connected or not published to. Do not show 0 — 0 views and not-published are different states.

**Summary at bottom:**
- Best platform for this episode: YouTube (4,821 views, $3.40 revenue)
- Weakest platform: Instagram (2,100 views, no revenue)
- Action suggestion: "YouTube is your highest-value platform for this content type. Instagram underperforms — consider reducing Instagram frequency for history content and increasing YouTube Shorts."

**Channel-level cross-platform view:**  
On the Analytics page, a tab showing channel-level platform comparison over the last 30 days. Total views per platform, average CTR (where applicable), revenue per platform, and a recommendation for platform investment priority.

---

## 9. Campaign Intelligence Requirement

A Campaign is a group of related episodes planned and measured together. Not every episode needs to be in a campaign, but grouping related episodes reveals patterns that per-episode analysis misses.

### Why campaign intelligence is critical before scaling

A factory that produces one episode at a time treats every episode as independent. This is fine for the first 10 episodes. After 20, 30, 50 episodes, it breaks down in two ways:

First, topic selection becomes reactive. Each new topic is chosen in isolation from what came before, leading to gaps, overlaps, and missed series opportunities. A 3-part series on Roman civilization that builds audience across three episodes generates more total views and retention than three unrelated one-off topics.

Second, performance analysis loses context. An episode about Pompeii that gets 8,000 views looks better than an episode about the Antikythera Mechanism that gets 4,000 views. But if the Antikythera episode is part of a "Greek Technology" campaign that collectively generated 24,000 views across 6 episodes, and the Pompeii episode has no follow-up, the campaigns tell a different story than the individual episodes.

Campaign intelligence must be built before the factory produces more than ~15 episodes. After that, the absence of grouping makes the episode list too long to scan and the performance data too flat to learn from.

### Required user-facing campaign fields

- Campaign name (e.g., "Greek Technology Series" or "Lost Civilizations Season 1")
- Episodes in campaign (list of episodes with their publish dates and statuses)
- Total production cost across all campaign episodes
- Total platform revenue across all campaign episodes
- Total views across all campaign episodes
- Net profit/loss for the campaign as a whole
- Strongest episode (highest CTR or views within campaign)
- Weakest episode (lowest performance within campaign)
- Best platform for this campaign (YouTube / TikTok / Instagram / Facebook)
- Campaign status: planning / in-production / complete / paused
- What to produce next: suggested next episode topic given campaign theme and gap analysis

**Campaign creation:** The operator should be able to group existing episodes into a campaign retroactively, or plan a new campaign with a target episode count and publish schedule before production begins.

---

## 10. Revenue Intelligence Requirement

Revenue is the measure of whether the factory pays for itself. It must be tracked at the episode level and the channel level, and the dashboard must make the economics visible without requiring the operator to calculate anything manually.

**Per-episode revenue:**
- Production cost — total AI and compute cost for this episode's pipeline run
- Platform revenue — earnings from YouTube AdSense / TikTok Creator Fund / Meta bonus programs (imported from analytics)
- Sponsor revenue — manually entered if the episode contains a sponsored segment
- Total revenue — sum of platform + sponsor
- Net profit/loss — total revenue minus production cost
- CPM — cost per mille impressions (revenue / impressions × 1000), if monetized
- RPM — revenue per mille views (revenue / views × 1000), if monetized

**Channel-level economics:**
- Production cost per month vs. budget ceiling
- Total revenue per month (all platforms combined)
- Net monthly profit/loss
- Break-even point — number of videos per month needed to cover production costs at current CPM
- Profitability trend — is the factory becoming more or less profitable over time?

**Profitable vs. unprofitable episodes:**  
A visual indicator on every episode: green (profitable), grey (no revenue data), red (cost exceeds revenue). The Recent Episodes table should include this indicator column.

**Revenue data sources:**
- YouTube AdSense: imported via YouTube Studio CSV export or API (revenue_per_video field)
- TikTok/Instagram/Meta: manual entry or CSV import where available
- Sponsorships: manual entry with a "Sponsor revenue" field on the episode record

**Display rule:**  
If the channel is not monetized, revenue fields show `—` with a grey "Not monetized" label. Never show $0.00 for a channel that hasn't applied for monetization — that implies zero earnings from an active program, which is different from no program at all.

---

## 11. Branding / On-Screen Identity Requirement

Every video produced by the factory must carry consistent, verifiable channel branding. This is not optional decoration — it is the visual proof that the content came from a legitimate, recognizable channel.

### Permanent Logo Bug / Watermark

A "logo bug" is the small channel logo overlaid permanently in a corner of the video, present throughout the entire duration. Every professional YouTube channel uses this. It:
- Makes the video recognizable even when shared without context
- Builds channel brand recall across views
- Is expected by audiences who subscribe to branded channels

**Required product behavior:**
- Upload: operator uploads channel logo in Settings / Channel Profile
- Position templates: top-left, top-right, bottom-left, bottom-right
- Size: small (5% of frame width) or medium (8% of frame width)
- Opacity: configurable (30% to 100%)
- Safe zone: auto-validated so the logo does not overlap subtitles or dynamic text regions

The logo bug must be applied to the final video export as a burned-in overlay, not as a platform-level feature. This ensures it appears regardless of which platform the video is published to.

### Per-Episode Dynamic Hashtag Overlay

In addition to the permanent logo, each video should display a brief hashtag or title card during the intro or outro, generated from the episode's content.

**Required product behavior:**
- Hashtags are generated by the pipeline from the episode topic, script, and channel niche
- The operator sees the generated hashtags on the episode detail page before the video is finalized
- The operator can edit, approve, or regenerate hashtags
- The hashtag overlay is positioned in the video frame using predefined templates (bottom-center during intro, top-right during outro)
- The overlay is burned into the video or applied as a platform-specific text overlay

### Safe Zone Validation

Before any text or graphic overlay is applied, the system must validate:
- The overlay does not cover face regions (if a face is present in the thumbnail or video frame)
- The overlay does not overlap with other required text regions (subtitles, lower-thirds)
- The overlay is within the platform-safe area (YouTube and TikTok crop differently on mobile)

### Preview Before Render

The operator must be able to preview the logo bug and hashtag overlay on a still frame from the video before the final render is triggered. This preview is a product requirement, not a nice-to-have. Without it, the operator has no way to verify branding placement without watching the full output video.

---

## 12. Platform Connection Requirement

### Current state is incorrect for all platforms

The current /platforms/connect/\<platform\> page stores a text string (labeled as "API key") for any platform. This does not correctly represent how YouTube, TikTok, Instagram, or Facebook authentication works.

**YouTube:**  
YouTube requires two distinct credential types:
1. A Data API key (for reading public video metadata, channel statistics, search). This is a server key — no OAuth required for read-only public data.
2. OAuth 2.0 credentials (for reading private channel analytics and for publishing). OAuth requires: a Google Cloud project, a client_id, a client_secret, and a redirect URI configured in the Google Cloud Console. The operator must complete a browser-based authorization flow. A token is returned and must be stored and refreshed.

The current implementation treats YouTube as a single credential field. It cannot do OAuth. Therefore it cannot access private analytics or publish videos. The "Connect YouTube" flow must be redesigned.

**TikTok:**  
TikTok Creator API uses OAuth 2.0 with TikTok's own developer portal. Requires a registered application, a client_key and client_secret, and a scope grant for video upload and analytics read. The access token expires and must be refreshed.

**Instagram / Facebook (Meta):**  
Meta uses the Graph API with OAuth. Requires: a Meta Developer App, a Page access token (for Instagram Business accounts), and specific permission scopes (instagram_basic, instagram_content_publish, pages_read_engagement). Instagram personal accounts cannot be connected — only Business or Creator accounts linked to a Facebook Page.

### Required platform connection UX

Each platform must have its own connection flow that reflects its actual authentication model:

**YouTube connection flow:**
1. Operator clicks "Connect YouTube"
2. System checks: is a Google Cloud project configured? If not, show step-by-step instructions for creating one and enabling the YouTube Data API.
3. Operator enters their client_id and client_secret from their Google Cloud project.
4. System generates the correct OAuth redirect URI and shows it to the operator: "Add this URL to your Google Cloud authorized redirect URIs: https://[factory-domain]/auth/google/callback"
5. "Authorize YouTube" button opens the Google OAuth consent screen in a new browser tab.
6. Operator grants access. Google redirects back with an authorization code.
7. Factory exchanges the code for access_token and refresh_token. Stores both.
8. Factory displays: "Connected to: [Channel Name] · [Channel ID] · Subscriber count: 12,400"
9. "Test connection" button fetches one week of analytics to confirm API access.

**TikTok connection flow:**  
Same OAuth pattern as YouTube but using TikTok's developer portal. Shows which API scopes were granted.

**Instagram/Facebook connection flow:**  
Same OAuth pattern via Meta. Shows which Facebook Page and Instagram Business account are linked.

**For all platforms — connection status display:**
- Connected: platform icon + channel name + subscriber/follower count + last successful sync timestamp + scopes granted
- Token expiring soon (< 7 days): warning with "Renew" button
- Token expired: error with "Reconnect" button
- Never connected: "Connect [Platform]" button

**Import fallback if API not connected:**  
If a platform is not connected via OAuth, the analytics import page shows: "YouTube is not connected via OAuth. You can still import analytics manually using CSV export from YouTube Studio." The manual import path remains available but is clearly labeled as a fallback.

---

## 13. Analytics Import Requirement

### Correct behavior by connection state

**If platform is connected via OAuth:**  
The analytics import page shows a dropdown of real videos uploaded to that channel. The operator selects a video. The factory fetches the analytics for that video via the API. The operator sees a preview of the data before confirming import. No CSV required.

```
Import Analytics — YouTube
─────────────────────────────
Your connected videos (most recent first):
○ The Antikythera Mechanism · Jun 14, 2026 · [thumbnail]
○ The Oracle at Delphi · Jun 7, 2026 · [thumbnail]
○ The Roman Forum · May 31, 2026 · [thumbnail]

[Fetch analytics for selected video →]
```

**If platform is not connected via OAuth (manual import):**  
Show CSV import as the primary option. JSON import as a secondary/developer option. Demo data generation behind an "Advanced" toggle or removed from the main flow entirely.

**Manual import must clearly mark source:**  
Every analytics record imported via CSV or JSON paste is tagged `source='manual'`. Every record generated via demo data is tagged `source='mock'`. These are different labels and must never be conflated. The executive dashboard always filters to `source IN ('manual', 'api')` — never `source='mock'`.

**Demo / mock data:**  
Demo data generation must be moved out of the main analytics import flow. It should be accessible from a /developer or /testing section only. On the main analytics import page, there is no "Generate demo data" option. If an operator has no real data, the page says: "No analytics available yet. [Import from YouTube Studio CSV] or [Connect YouTube via OAuth to fetch automatically]."

**Import modes:**
1. API (auto-fetch) — platform connected, factory fetches on demand or on schedule
2. CSV — operator exports from platform dashboard, pastes or uploads here
3. Manual (video URL) — operator pastes a YouTube URL, factory fetches public analytics (views, likes) via the public Data API key — no OAuth required for public data
4. JSON — developer option, behind Advanced toggle

**Matching imported analytics to episodes:**  
When an operator imports analytics for a YouTube video, the system must attempt to match the video to an existing episode by platform_video_id. If no match is found: "This video (dQw4w9WgXcQ) is not linked to any episode. Do you want to create an episode record for it?" The operator should not need to know the internal episode ID.

---

## 14. Current Schema Gap Re-evaluation

After the product audit, these are the schema areas that require redesign. This section identifies what the product needs — not what to build yet.

**Episode core object:**  
The `episodes` table represents a pipeline run output, not a product-level episode. It has `title`, `topic`, `status` (pipeline status), and `run_id`. It does not have: published title (vs. internal topic), platform-specific status per platform, logo bug / watermark applied, final video file path, script text, hashtags. The episode must be redesigned as a content object first, with the pipeline run as a subordinate record.

**Publishing outcomes:**  
The `publishing_outcomes` table has one row per episode × platform with `platform_video_id`, `url`, `published_at`, and `publishing_status`. This is close to correct. Missing: title at publish time, description at publish time, hashtags at publish time, thumbnail_id published with this video, platform-specific customizations (TikTok captions differ from YouTube descriptions).

**Platform analytics:**  
The `platform_analytics` table is structurally sound but missing: `channel_id` (blocks channel-level baseline computation), `revenue_usd`, `subscriber_gain`, `retention_curve`, `performance_band` fields. These are needed by the product surfaces defined in §6–8 of this document.

**Thumbnail candidates / results:**  
The `thumbnail_candidates` table has the design inputs (prompt, placement, text_overlay, selected flag) but no output linkback: no image file path, no post-publish CTR outcome, no style classification. The product requires a bidirectional record: from design to publish to performance.

**Title candidates / results:**  
Same problem as thumbnails. `titles` has the candidate text but no published-title linkback, no title format classification, no CTR outcome. The product cannot show title intelligence without these.

**Campaigns:**  
No campaign concept exists in the schema at all. The product definition in §9 defines what campaigns need. Schema follows.

**Revenue:**  
No revenue fields exist on `platform_analytics`. The `channel_profile_settings` table has no monetization fields. The product requires: `revenue_usd` on platform analytics, monetization status on channel profile, manual sponsor revenue on episode record.

**Branding overlays:**  
No schema exists for logo bug position, hashtag overlay, or safe-zone configuration. This is a new product capability that requires its own table or extension to channel_profile_settings.

**Platform credentials / connections:**  
The `secret_profiles` table stores credentials as opaque text blobs. It has no: OAuth access token, refresh token, token expiry, authorized scopes, connected channel ID, last successful sync. The platform connection redesign (§12) requires these fields.

---

## 15. Priority Plan

### P0 — Must build before the first video from the dashboard

These are blocking. The factory cannot be called "launched" until every P0 item is complete.

**Real executive dashboard:**  
The /dashboard must show the target layout from §4. Channel identity bar, warnings bar, last published video with thumbnail, production status, 5 KPIs, publishing targets vs. actuals, cost/revenue card, charts, recent episodes with thumbnails. No nav item that requires explanation.

**Channel profile (complete):**  
The /channel-profile form must collect: channel name, description, niche, tone, brand colors, logo upload, monthly episode target, subscriber goal, monetization status. Publishing targets per platform as in current form. Save to channel_profile_settings.

**Platform connection UX (YouTube first):**  
The /platforms/connect/youtube must implement the correct OAuth flow as described in §12. Operator can connect their YouTube account, factory confirms the connection by fetching channel name and subscriber count. All other platforms can remain as manual import for now.

**Manual analytics import (clear and correct):**  
The /analytics/import page must remove "Generate demo data" as a default option, show CSV import as primary, and match imported data to episodes by platform_video_id. No mock data in the main flow.

**Published video registration:**  
The /published/import form must allow registering a YouTube video by URL (not by internal episode ID). The factory parses the URL, extracts the video ID, fetches public metadata (title, thumbnail, publish date), and pre-fills the form.

**Episode detail page:**  
Every episode must have a detail page at /episode/\<episode_id\> showing: the episode object fields from §5, thumbnail candidates with images, title candidates with selected indicator, publishing status per platform, analytics summary, production cost, revenue, net profit/loss.

**Thumbnail / title candidate display:**  
The episode detail page must render actual thumbnail images (not file paths) and display all title candidates. The published thumbnail and title must be clearly marked.

**Production launch from dashboard:**  
The main dashboard must have a "Start production" button that launches a new episode run from the queued topic. No command line required. The operator picks a topic from the queue (or types a new one), clicks Start, and the run begins. The live production panel updates.

**Clear warnings only:**  
The warnings bar on the main dashboard must show only operator-language warnings. No OIS signal types. No "gate verdict cluster." Only: content health (last published N days ago), credential health (expires in N days), performance health (CTR dropped X%), cost health (budget at Y%).

**Hide debug/OIS under Operations:**  
All pages currently reachable from the main nav that are developer-facing — /signals, /decision-inputs, /decisions, /decision-reviews, /execution-plans, /preflight — must move behind a collapsed "Operations" section or to a /operations sub-navigation. They remain accessible but not primary.

---

### P1 — Must build during the first month of automation

These are required for the factory to learn and improve without manual intervention during the first 30 automated days.

**Learning loop:**  
The intelligence engine must fire after each episode's analytics are imported, generate a plain-language insight, and surface it on the main dashboard learning card (§4 Row 10). The operator must be able to apply or dismiss the insight from the dashboard without navigating to /learning.

**Thumbnail / title performance comparison:**  
The episode detail page must show the thumbnail comparison (§6) and title comparison (§7) views with real CTR data linked to the published selection.

**Cross-platform comparison:**  
The episode detail page must show the cross-platform performance view (§8) for any episode published to multiple platforms.

**Revenue / cost tracking:**  
The cost/revenue card on the main dashboard (§4 Row 7) must show real data. Revenue import via YouTube Studio CSV must be working and mapped to episode records. Net profit/loss must be computed and displayed.

**Campaign grouping:**  
The operator must be able to create a campaign, add episodes to it, and see campaign-level totals (§9). This is the minimum campaign intelligence surface.

**Monthly stability fixes:**  
Every failure that surfaces during the 30-day automation run must be fixed before Mac mini migration. This includes: credential token refresh, failed run retry from the dashboard, analytics import errors, publishing failures.

**Mac mini migration readiness:**  
The dashboard must be accessible over the local network. The factory must survive machine restarts (systemd / launchd service). The database must be on a persistent, backed-up path.

---

### P2 — V2 media-company expansion

These are post-stability, post-month-one, post-migration work.

**Multi-channel / multi-client:**  
Multiple channels per factory. Channel-to-channel isolation. Per-channel user access. Client portal view showing only the client's assigned channels.

**Access control:**  
Role-based access: Owner, Admin, Operator, Editor, Viewer, Client. Role enforcement at the API layer. Client role with channel-scoped filtering.

**White-label:**  
Factory branding applied to the dashboard (logo, accent color, dashboard title). "Factory OS" strings replaced with operator-configured name. Custom domain support.

**SaaS / private cloud / local deployment modes:**  
The current product is local-only. V2 must support: local (current), private cloud (VPS, Docker), and SaaS (hosted multi-tenant). Each deployment mode has different credential storage, different backup requirements, and different authentication models.

**Media-company dashboard presets:**  
The V2 dashboard is not a YouTube-only product. It must support: podcast networks, newsletter publishers, short-form video factories, hybrid content operations. Different dashboard presets for different content operation types.

---

## 16. Final Recommendation

**Stop schema expansion. Build the product first.**

The current schema is sufficient to build every P0 product surface defined in this document. The `episodes`, `platform_analytics`, `thumbnail_candidates`, `titles`, `publishing_outcomes`, and `channel_profile_settings` tables already exist. The data is there or can be added via P0 product work without schema changes.

The four schema extensions that are genuinely blocking P0 work — `channel_id` on `platform_analytics`, `revenue_usd` on `platform_analytics`, image file paths on `thumbnail_candidates`, and OAuth credential fields on `secret_profiles` — are narrow, targeted additions. They are not the 47-column multi-table migrations described in F11_GAP_ANALYSIS.md.

The gap analysis document produced before this one identified real gaps. But it identified them from the schema's perspective, not the operator's. When you ask "what does the schema need to support full F11 intelligence," you get 47 columns. When you ask "what does an operator need to see before the factory produces its first video from the dashboard," you get a much smaller list.

The correct sequence is:

1. Build the /dashboard as defined in §4. Wire up the existing data. Do not add new tables.
2. Build the episode detail page as defined in §5. Wire up existing thumbnail_candidates and titles tables. Add image file path to thumbnail_candidates.
3. Fix the YouTube connection flow to use OAuth. Add token fields to secret_profiles.
4. Add `revenue_usd` and `channel_id` to `platform_analytics`. These two columns unlock the KPI strip and the cost/revenue card.
5. Launch the factory. Run for 30 days.
6. Then — based on what is actually missing from real operation, not from design — decide what schema to add next.

Schema expansion driven by product requirements discovered through real operation is sound. Schema expansion driven by architectural speculation in advance of any real use is waste. The project has enough schema. It needs product.

---

*F11_PRODUCT_GAP_ANALYSIS.md — 2026-06-19*  
*Audit only. No schema changes. No migrations. No code. No template modifications.*
