# Episode Detail Page — Product Specification
## /episode/<episode_id>

**Date:** 2026-06-19  
**Phase:** F11 P0  
**Type:** Product design only. No code. No schema changes. No migrations.  
**Status:** Spec for implementation.

---

## 1. Episode Page Purpose

### Why Episode becomes the central object

Every piece of intelligence, performance data, and creative output the factory produces is ultimately about a single thing: a video. That video has a title. It has a thumbnail. It was published somewhere. It earned some amount of watch time, revenue, and audience attention. And everything the factory learned from it applies to the next video it will make.

Today, that single object — the video — does not have a home in Factory OS. Instead, its information is scattered across five pages, none of which are linked to each other:

- **Production information** lives on `/run/<run_id>` — showing stage timelines, gate verdicts, costs in a technical audit log format, identified by a truncated UUID.
- **Analytics information** lives on `/analytics/episode/<episode_id>` — showing a cross-platform metrics table and insight badges.
- **Thumbnail candidates** are stored in the database but have no display page at all.
- **Title candidates** are stored in the database but have no display page at all.
- **Learning actions** are split between `/analytics/episode/<episode_id>` and `/learning` — with no connection to the specific episode that generated them.
- **Publishing status** lives in `publishing_outcomes` with no dedicated display.

An operator who wants to understand a single video must navigate to five different URLs, none of which are linked from the dashboard, and none of which show the information they most need — the thumbnail and the published title — at all.

The Episode Detail page fixes this by making the episode the primary unit of the product. Every piece of data about a video — production, publishing, thumbnails, titles, analytics, revenue, learning, and branding — lives under one URL, organized into tabs that match the operator's mental model.

### Why dashboard should summarize

The dashboard's job is to answer: "Is everything healthy? What needs my attention?" It should show the most recent episode, the most important KPIs, the most urgent warnings, and the current production state. It should surface the signal without the detail.

Once the Episode Detail page exists, the dashboard Recent Episodes table becomes a summary row: thumbnail, published title, platform status, CTR, and a link to the Episode Detail page. The dashboard does not need to contain everything. It needs to surface what matters and route the operator to where they can act.

### Why Episode page should contain all details

The Episode Detail page is the accountability surface for the factory. When an episode performs poorly, the operator needs to see: what thumbnail was generated, what the prompt was, what CTR it achieved, what titles were offered, which one was chosen, what the publishing timeline looked like, what the analytics show, what the factory learned, and what it recommends changing next time. All of that must be one navigable unit — not a scavenger hunt across five pages.

The Episode Detail page also becomes the operator's quality review tool before a video is published. Before an episode goes live, the operator can open the Episode Detail page, review the production results, inspect the three thumbnail candidates, approve the title, and confirm publishing — all without touching a terminal.

### Which current pages become secondary after Episode exists

**Pages that lose their primary role:**

- `/run/<run_id>` — becomes an Operations-only technical audit log. Linked from the Episode Detail Production tab's "Advanced / Run log" expandable section. Not in the primary nav.
- `/analytics/episode/<episode_id>` — merged entirely into the Episode Detail Analytics and Learning tabs. The route remains for backward compatibility but redirects to `/episode/<episode_id>?tab=analytics`.
- `/learning` — the global intelligence page remains for channel-level recommendations, but per-episode learning moves to the Episode Detail Learning tab. The learning page's per-episode action cards are replaced with links to the Episode Detail page.

**Pages that remain unchanged:**

- `/dashboard` — updated to link episode rows to `/episode/<episode_id>`, not `/run/<run_id>`.
- `/analytics` — the portfolio overview remains for channel-level cross-episode comparison.
- `/analytics/platform/<platform>` — remains for platform-level views.

**Pages that disappear entirely:**

- The `/episodes` list page becomes the Episode Index: a simple list of episodes with a thumbnail, title, status, and a link to the Episode Detail page. The "Enqueue Topic" form moves to `/queue`.

---

## 2. Episode Header

The header is the most important part of the page. It must answer in under 3 seconds: what is this video, where does it stand, and what can I do with it?

### Layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  [Thumbnail]    The Antikythera Mechanism: Ancient Greek Computer           │
│   160×90px      Topic: Ancient Greek technology and astronomy               │
│                 ep_003 · Created Jun 9 · Published Jun 14 · 6 days ago     │
│                                                                             │
│                 ● Published      YouTube · TikTok                           │
│                 Stage: Complete · 26/26 stages passed                       │
│                                                                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                                   │
│  │  $11.80  │ │  $3.40   │ │  -$8.40  │                                   │
│  │  Cost    │ │ Revenue  │ │   Loss   │                                   │
│  └──────────┘ └──────────┘ └──────────┘                                   │
│                                                                             │
│  [▶ Open Video]  [↻ Re-render]  [↑ Re-publish]  [⊞ Regenerate Thumbnail]  │
│  [T Regenerate Title]  [✦ View Learning]                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Header fields — definitions

**Thumbnail preview:**  
The selected (published) thumbnail displayed as a 160×90 image at 16:9 ratio. If no thumbnail is available, a grey placeholder with "No thumbnail" is shown. Clicking the thumbnail navigates to the Thumbnail tab.

**Episode title:**  
The published title — the title that was selected and submitted to the platform. Not the internal topic string. Not the episode ID. If no title has been selected yet (pre-publish), show the topic with a "Draft title" label. Font: large, bold, 18–20px. Full text, not truncated.

**Topic:**  
The original topic brief given to the pipeline. Displayed below the title in muted text: "Topic: Ancient Greek technology and astronomy." This distinguishes the operator's input from the factory's output.

**Episode metadata line:**  
One line, muted, showing: episode ID (in a monospace chip, subtle — not the focal point) · Created date · Published date (or "Not published") · human-readable age ("6 days ago").

**Platform status pills:**  
One pill per connected platform, showing: green dot + platform name if published, grey dot + "Not published" if not, red dot + "Failed" if failed. Platforms not enabled are omitted entirely.

**Stage indicator:**  
"Stage: Complete · 26/26 stages passed" for a completed episode. "Stage: Visual Production (12/26)" for an episode in progress. "Stage: Failed at Voice Synthesis" for a failed episode. This is the production health in one line.

**Economic KPIs:**  
Three side-by-side metric cards inline with the header: Production Cost (total from costs table) · Revenue (total from platform_analytics revenue fields, or "—" if no data) · Net (revenue minus cost, shown in green if positive, red if negative, grey if revenue unknown). Each card shows the number prominently and a label below.

**Quick actions:**  
A horizontal action bar below the KPI cards. All actions are buttons with clear icons and labels. Destructive actions (Re-render, Re-publish) have a confirmation step before executing.

### Quick actions — detailed definitions

**Open Video:**  
Primary action. Opens the published video on its platform (YouTube, TikTok, etc.) in a new tab. If published to multiple platforms, shows a dropdown: "Open on YouTube" / "Open on TikTok." Disabled and greyed out if not published.

**Re-render:**  
Queues a new production run for this episode using the same topic and configuration. Shows a confirmation: "This will start a new production run for this episode. Estimated cost: ~$12. Continue?" Disabled if a run is already active.

**Re-publish:**  
Attempts to re-upload the existing final video to the connected platform. Does not re-render. Useful when publishing failed due to a credential or rate-limit issue, not a production issue. Shows confirmation.

**Regenerate Thumbnail:**  
Opens the Thumbnail tab and triggers a new generation cycle for three new candidates. Does not replace the published thumbnail automatically — the operator selects which one to use. Confirmation: "This will generate 3 new thumbnail options for this episode. Estimated cost: ~$0.30. Continue?"

**Regenerate Title:**  
Opens the Title tab and triggers a new generation cycle for additional title candidates. Same pattern as thumbnail — operator selects, does not auto-replace.

**View Learning:**  
Navigates to the Learning tab of this Episode Detail page. The button is highlighted (blue border or blue text) if there are unapplied learning actions from this episode.

---

## 3. Production Tab

The Production tab shows the full lifecycle of how this episode was made. It is organized as a visual pipeline — not a raw log table. The operator sees what ran, what it produced, how long it took, and how much it cost.

### Layout concept

```
PRODUCTION  ─────────────────────────────────────────── Run: Jun 9, 14:22  ─ 42 min  ─ $11.80

  STAGE PIPELINE

  ✓ Research          6 min  ·  $0.14   [Sources: 8]                            [View artifacts]
  ✓ Script            4 min  ·  $0.62   [Words: 2,840]                          [View script]
  ✓ Voiceover         2 min  ·  $0.38   [Duration: 9:42]                        [Play audio]
  ✓ Thumbnail Plan    1 min  ·  $0.08   [3 candidates planned]                  [View]
  ✓ Image Generation  9 min  ·  $1.20   [3 thumbnails]                          [View images]
  ✓ Visual Production 18 min ·  $8.90   [26 scenes · 26 clips]                  [View clips]
  ✓ Assembly          2 min  ·  $0.38   [Final video: 9:42]                     [View video]
  ✓ Brand Guardian    1 min  ·  $0.08   [Passed: all gates]                     [View verdict]
  ✓ Publishing        —      ·  —       [YouTube: success · TikTok: success]    [View status]

  Total: 43 min  ·  $11.80

  ─────────────────────────────────────────────────────────────────────────────
  ▸ Advanced / Run log   (opens stage-level technical detail, gate verdicts, state transitions)
```

### Stage row fields

Each stage is one row in a vertical timeline. Completed stages show a green check. Failed stages show a red X and display the error. Skipped stages show a grey dash. Running stages show a spinner animation.

Each row shows:
- **Stage name** — human-readable. "Research" not `RESEARCH_DIRECTOR`. "Voiceover" not `ELEVENLABS_TTS_STAGE`.
- **Duration** — "6 min" or "42 sec." If running, shows elapsed time live.
- **Cost** — "$0.14" or "—" if no billable call.
- **Artifact summary** — a brief, human-readable summary of what this stage produced. Not raw schema values. "Sources: 8" (not `entity_count: 8`). "Words: 2,840" (not `word_count: 2840`). "3 thumbnails" (not `asset_type: thumbnail_candidate, count: 3`).
- **View link** — opens the artifact inline (image, audio player, script text, video player) as an expandable panel below the stage row, not in a new tab.

### Stage definitions — human-readable names

| Internal stage name | Display name | Artifact summary |
|---|---|---|
| RESEARCH_DIRECTOR | Research | Sources found · Entities extracted |
| SCRIPT_WRITER | Script | Word count · Estimated duration |
| NARRATION_DIRECTOR | Voiceover | Audio duration · Voice used |
| THUMBNAIL_PLAN | Thumbnail Plan | Candidates planned |
| IMAGE_GENERATION | Image Generation | Thumbnails generated |
| VPD (Visual Production) | Visual Production | Scenes · Clips |
| ASSEMBLY | Assembly | Final video duration |
| BGA_PASS_1 / BGA_PASS_2 | Brand Guardian | Pass / Warnings / Blocked |
| PUBLISHING | Publishing | Platform · Status |

### Failed stage display

When a stage failed, the row expands automatically to show the error in operator language:

```
  ✗ Visual Production    18 min ·  $4.20   [Failed at scene 7/26]

    What happened: The video generation provider (Seedance) returned an error
    for scene 7. Scenes 1–6 were completed successfully.

    Error: rate_limit — daily credit limit reached
    Credits used this run: 47 / 50

    Options:
    [↻ Retry from this stage]   [↻ Re-render from start]   [View provider status]
```

The error message is translated from the internal error type (`rate_limit`) into operator language. Raw exception text is hidden behind a "Show technical detail" toggle for developers.

### Script viewer

When the operator clicks "View script" on the Script stage, the script text expands inline:

```
  ✓ Script   4 min · $0.62   [Words: 2,840]   [▼ Hide script]

  ─────────────────────────────────────────────────────────
  SCRIPT — The Antikythera Mechanism

  [HOOK]
  Imagine holding in your hands a device so advanced that it wouldn't
  be reinvented for another 1,400 years...

  [SCENE 1 — ESTABLISH]
  In 1900, a Greek sponge diver named Elias Stadiatos surfaced from the
  waters off the island of Antikythera...
  ─────────────────────────────────────────────────────────
```

### Final Video player

The Assembly stage "View video" link opens an embedded video player inline:

```
  ✓ Assembly   2 min · $0.38   [Final video: 9:42]   [▼ Show video]

  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │              [▶  Play  ─────────────── 9:42]           │
  │                                                         │
  └─────────────────────────────────────────────────────────┘
  File: .hypogeum-assets/ep_003/assembly/ep_003_final.mp4
  Size: 842 MB  ·  Resolution: 1920×1080  ·  FPS: 24
```

### Advanced / Run log

Collapsed by default. Contains the technical detail currently shown on `/run/<run_id>`: run ID, state transitions, gate verdicts with flag counts, raw cost records per API call, asset file paths. This is the content that developers and engineers need for debugging. Operators never need to see it during normal operation.

---

## 4. Publishing Tab

The Publishing tab answers: where did this video go, did it arrive, and what is the current status on each platform?

### Layout

```
PUBLISHING  ────────────────────────────────────────────────────────────────

  ┌────────────────────────┬────────────────────────┬────────────────────────┐
  │  ● YouTube             │  ● TikTok              │  ○ Instagram           │
  │  Published             │  Published             │  Not published         │
  │                        │                        │                        │
  │  Jun 14, 2026          │  Jun 14, 2026          │  —                     │
  │  2:34 PM UTC           │  3:12 PM UTC           │                        │
  │                        │                        │  [Publish to Instagram]│
  │  ID: dQw4w9WgXcQ       │  ID: 7891234567        │                        │
  │  [Watch on YouTube ↗]  │  [Watch on TikTok ↗]   │                        │
  │                        │                        │                        │
  │  Upload: 4m 12s        │  Upload: 2m 08s        │                        │
  │  Last sync: 2h ago     │  Last sync: 2h ago     │                        │
  │  [Sync now]            │  [Sync now]            │                        │
  └────────────────────────┴────────────────────────┴────────────────────────┘
```

### Per-platform card fields

**Header:**  
Platform name with a status dot: green (published), yellow (pending/uploading), red (failed), grey (not published to this platform). The dot and status label are the most prominent element.

**Publish date and time:**  
Full date and time in the operator's local timezone. "Jun 14, 2026 · 2:34 PM UTC." If not published: "—."

**Platform Video ID:**  
Shown in a subtle monospace chip below the date. Not the raw string on its own — formatted as "ID: dQw4w9WgXcQ" with a copy-to-clipboard icon. Clicking the ID opens the video on the platform.

**Direct platform URL:**  
A "Watch on YouTube ↗" link that opens the video in a new tab. Clear label — not "View," not "Open," but the platform name. If the video failed to publish, this link is absent.

**Upload duration:**  
How long the upload took. "4m 12s." Shows only for completed uploads.

**Last sync:**  
When the factory last fetched analytics from this platform. "2h ago" with a "[Sync now]" button that triggers an analytics fetch (or opens the analytics import page if not connected via OAuth).

**Error display:**  
When publishing failed, the card shows the failure reason in operator language:

```
  ✗ YouTube
  Publishing failed

  Reason: YouTube credential expired — re-authorization required.

  [Reconnect YouTube]   [Retry publishing]
```

### Side-by-side layout behavior

If only one platform: single full-width card.  
If two platforms: two equal columns.  
If three platforms: three equal columns.  
If four platforms: two-by-two grid.  
Platform cards for platforms not enabled on the channel are omitted entirely — not shown as empty disabled cards.

### Scheduling (pre-publish state)

When an episode is complete but not yet published, the Publishing tab shows scheduling controls:

```
  READY TO PUBLISH

  This episode has been produced and is ready to publish.

  ┌─────────────────────────────────────────────────────────┐
  │  YouTube                                                │
  │  ○ Publish now                                          │
  │  ● Schedule: [Jun 17, 2026]  [09:00 AM] [UTC ▼]       │
  │                                                         │
  │  TikTok                                                 │
  │  ● Publish now                                          │
  │  ○ Schedule: —                                          │
  └─────────────────────────────────────────────────────────┘

  [Confirm and Publish]
```

---

## 5. Thumbnail Tab

The Thumbnail tab is the visual intelligence center for this episode. It must make visible what the factory generated, what was chosen, and what the audience responded to.

### Layout overview

```
THUMBNAILS  ────────────────────────────────────────────────────────────────

  CANDIDATES

  ┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
  │                     │  │                     │  │                     │
  │   [Thumbnail A]     │  │   [Thumbnail B]     │  │   [Thumbnail C]     │
  │    320 × 180px      │  │    320 × 180px      │  │    320 × 180px      │
  │                     │  │                     │  │                     │
  └─────────────────────┘  └─────────────────────┘  └─────────────────────┘
  ★ PUBLISHED              ○ Not used               ○ Not used

  Thumbnail A (published)
  ─────────────────────────────────────────────────────────────────────────
  Prompt:  "A bronze gear mechanism floating in dark water, ancient Greek
            aesthetic, dramatic side lighting, text overlay: ANCIENT GREEK
            COMPUTER, bold white text, top placement"

  Model: gpt-image-2-text-to-image
  Seed: 4829110   Cost: $0.10   Generated: Jun 9, 14:28

  PERFORMANCE
  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────────────┐
  │  120,400 │  │   5.2%   │  │  4,821   │  │  → On baseline           │
  │Impressions│  │   CTR    │  │  Views   │  │  vs channel avg 5.1%    │
  └──────────┘  └──────────┘  └──────────┘  └──────────────────────────┘

  ─────────────────────────────────────────────────────────────────────────

  Thumbnail B        [View prompt ▼]
  Not published · No performance data

  Thumbnail C        [View prompt ▼]
  Not published · No performance data

  ─────────────────────────────────────────────────────────────────────────

  RECOMMENDATION FOR NEXT EPISODE

  ✦ The published thumbnail used a "dark background + artifact close-up"
    composition. This is consistent with your last 3 above-average episodes.
    For the next episode, continue with: dark background, single subject,
    high contrast text, top placement.

  Based on 6 episodes with real CTR data.

  [Apply to next episode]   [View all thumbnail learnings →]
```

### Candidate card display

Each candidate is displayed as a visual card with the thumbnail image as the centerpiece. Images are rendered at 320×180 pixels minimum. The operator can click any candidate image to open it in a full-screen lightbox.

**Published badge:**  
The selected (published) candidate has a "★ PUBLISHED" badge below it in green. Non-selected candidates show "○ Not used" in grey.

**Candidate detail section:**  
Below the three candidate images, each candidate has a collapsible detail panel. The published candidate is expanded by default. Others are collapsed with a "[View prompt ▼]" toggle.

**Detail panel fields:**

- **Prompt:** The full generation prompt in a readable block. Not truncated. Wrap at 80 characters.
- **Model:** The image model used. Human-readable: "gpt-image-2-text-to-image" displayed as "GPT Image 2 · Text-to-Image."
- **Seed:** The generation seed if available, shown as a subtle value. Includes a "Regenerate with same seed" link that creates a new candidate using the same parameters.
- **Cost:** "$0.10" — the cost of generating this specific thumbnail candidate.
- **Generated:** Date and time when this candidate was generated.

**Performance section (published thumbnail only):**

Four KPI cards:
1. Impressions — how many times this thumbnail was shown
2. CTR — click-through rate. Color-coded: red below 2%, yellow 2–4%, green above 5%.
3. Views — total view count from CTR clicks (this is views, not impressions)
4. Performance band — "Outperforming / Above average / On baseline / Below average / Underperforming" compared to channel 30-day average CTR. With the exact channel average shown: "vs channel avg 5.1%."

Non-published candidates show: "Not published · No performance data" in muted text. They are still shown with their full prompt and generation details — this is valuable for the intelligence engine to learn what options were available.

**Retention impact (when data available):**

Below the four KPI cards, if retention data is available:

```
  Retention: 62% avg view  ↑ Above channel average (58%)
  Viewers who clicked this thumbnail watched more of the episode
  than the channel average — positive signal for this thumbnail style.
```

### Winner / Loser display

The published thumbnail shows either:
- A green "★ Winner" badge if CTR is above channel average
- A grey "→ Baseline" badge if CTR is within ±5% of channel average
- A red "▼ Below target" badge if CTR is more than 5% below channel average

These badges are only shown when real analytics data exists. They are absent when no analytics have been imported.

### Recommendation

The recommendation block at the bottom of the Thumbnail tab synthesizes the current episode's performance with the channel's thumbnail history. It is written in plain operator language.

When fewer than 3 episodes have thumbnail performance data: "Not enough data yet — recommendation will appear after 3 episodes have real CTR data."

When the episode has no analytics: "Analytics not yet imported — [Import analytics →] to unlock thumbnail intelligence."

---

## 6. Title Tab

The Title tab mirrors the Thumbnail tab's structure for titles.

### Layout overview

```
TITLES  ────────────────────────────────────────────────────────────────────

  CANDIDATES

  ★ PUBLISHED
  The Antikythera Mechanism: Ancient Greek Computer
  Type: Statement · 48 characters · 7 words

  ○ NOT USED
  Did Ancient Greeks Build a Computer? The Antikythera Secret
  Type: Question · 59 characters · 9 words

  ○ NOT USED
  Inside the World's Oldest Computer: Ancient Greece's Greatest Mystery
  Type: Mystery / Number-free · 68 characters · 11 words

  ○ NOT USED
  The 2,000-Year-Old Computer That Shocked the World
  Type: Number Lead · 51 characters · 9 words

  ─────────────────────────────────────────────────────────────────────────

  PERFORMANCE — Published title

  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────────────┐
  │  120,400 │  │   5.2%   │  │  4,821   │  │  → On baseline         │
  │Impressions│  │   CTR    │  │  Views   │  │  vs channel avg 5.1%  │
  └──────────┘  └──────────┘  └──────────┘  └────────────────────────┘

  Retention impact: 62% avg view · On baseline (channel avg 58%)

  ─────────────────────────────────────────────────────────────────────────

  TITLE PATTERN ANALYSIS — Channel history

  Question titles:     5 episodes  ·  avg CTR  6.1%  ↑
  Statement titles:    3 episodes  ·  avg CTR  4.8%  →
  Number Lead titles:  2 episodes  ·  avg CTR  5.8%  ↑
  Mystery titles:      1 episode   ·  avg CTR  4.2%  ↓

  Strongest pattern: Question titles (+27% vs statement average)

  RECOMMENDATION FOR NEXT EPISODE

  ✦ Question-format titles outperform your other title types by 27% CTR.
    The published title for this episode used a Statement format.
    Consider testing a Question format for the next episode with this topic.

  Based on 11 episodes with real CTR data.

  [Apply pattern to next episode]   [View all title learnings →]
```

### Candidate display fields

Each candidate title is a row, not a card (titles are text — image layout is wrong for text).

Each row shows:
- Title text — full text, not truncated, in large readable font
- Type classification — Question / Statement / Number Lead / Mystery / How-To / Listicle. Auto-classified by the product layer, not manually entered.
- Character count and word count — small, muted, below the title
- Published badge (if selected) or "Not used" (if not selected)

Non-published candidates have no performance data. They are shown as a reference for pattern analysis only. The key value of showing rejected candidates is that the intelligence engine can compare what was tried vs. what was chosen vs. what performed.

### Generation source field

Each candidate shows where it came from:
- "Generated by pipeline" — created during the production run
- "Manual entry" — the operator typed this title themselves
- "Regenerated" — created after the initial run via the "Regenerate Title" action

### Performance section

Identical structure to the Thumbnail tab performance section: four KPI cards (Impressions, CTR, Views, Performance band) plus Retention impact. Shown only for the published title. All other candidates show "Not published · No performance data."

### Title pattern analysis

This section only appears when the channel has at least 3 episodes with real CTR data. It shows a simple table: title format type, episode count, average CTR, trend arrow.

Below the table, one line identifies the strongest pattern: "Question titles (+27% vs statement average)."

The recommendation block then applies this to the current episode: if the current episode used the strongest pattern, confirm it; if it used a weaker pattern, flag that a stronger option was available.

### Recommendation engine state

Three states:
1. **Enough data, analytics imported:** Full recommendation with evidence episode count.
2. **Enough data, no analytics for this episode:** "Analytics not yet imported. Import analytics to see title performance for this episode."
3. **Not enough data:** "Title pattern analysis requires at least 3 episodes with CTR data. Currently X episode(s) with data."

---

## 7. Analytics Tab

The Analytics tab shows cross-platform performance data for this specific episode. It is the single place to compare how the same video performed across different platforms and audiences.

### Layout overview

```
ANALYTICS  ─────────────────────────────────────── Last synced: Jun 19, 11:40 AM · [Sync now]

  DATA SOURCE
  ● YouTube: real data (imported Jun 16)    ○ TikTok: real data (imported Jun 16)
  ○ Instagram: no data                      ○ Facebook: not connected

  ─────────────────────────────────────────────────────────────────────────

  PLATFORM COMPARISON

              YouTube          TikTok          Instagram       Facebook
  ─────────────────────────────────────────────────────────────────────────
  Views       4,821            12,400          —               —
  Impressions 120,400          —               —               —
  CTR         5.2% →           N/A             —               —
  Retention   62% ↑            34%             —               —
  Watch time  182h total       8.1h total      —               —
  Avg session 2m 16s           0m 39s          —               —
  Likes       312              890             —               —
  Comments    28               47              —               —
  Shares      64               240             —               —
  Saves       —                210             —               —
  Subs gained 22               —               —               —
  Revenue     $3.40            —               —               —
  ─────────────────────────────────────────────────────────────────────────
  Snapshot    Jun 16           Jun 16          —               —

  ─────────────────────────────────────────────────────────────────────────

  CHARTS

  Views by platform       [Bar chart — YouTube: 4821, TikTok: 12400]

  CTR trend                [Line chart — this episode's CTR vs. last 10 episodes]

  Retention by platform   [Bar chart — YouTube: 62%, TikTok: 34%]

  ─────────────────────────────────────────────────────────────────────────

  VERDICT

  Best platform:   TikTok  (12,400 views vs YouTube 4,821)
  Best engagement: TikTok  (890 likes, 240 shares)
  Best retention:  YouTube (62% avg view vs TikTok 34%)
  Best revenue:    YouTube ($3.40)

  Recommendation: This content performed 2.5× better on TikTok by view count
  but had stronger retention on YouTube. For historical content, YouTube
  retention is a stronger signal. Prioritize YouTube distribution for this
  content type.
```

### Platform comparison table behavior

- Cells show "—" when no data has been imported for that platform.
- Cells show "N/A" when the metric doesn't apply to the platform (e.g., TikTok doesn't have a traditional CTR metric in the same format as YouTube).
- Color coding on CTR: red below 2%, yellow 2–4%, green above 5%.
- Color coding on retention: red below 30%, yellow 30–50%, green above 50%.
- Platform columns are shown only for connected or previously-published platforms. Not-enabled platforms are omitted.

### Data source indicator

Above the table, a status bar showing each platform's data freshness:
- "real data (imported Jun 16)" — green dot, import date
- "no data" — grey dot, with "[Import analytics]" link inline
- "not connected" — grey dot, with "[Connect platform]" link inline
- "demo data" — yellow dot with "(excluded from this view)" note

Demo data is never shown in the Analytics tab. The tab shows only real or manually-imported data.

### Charts

Three charts, stacked vertically:

**Views by platform:** A horizontal bar chart with one bar per platform. Easy to see which platform drove the most views. Bars are labeled with the exact number.

**CTR trend:** A line chart showing this episode's CTR (plotted as a point) against the last 10 episodes' CTR (as a line). Context: is this episode's CTR improving the channel or pulling it down? Only shown when at least 3 prior episodes have CTR data.

**Retention by platform:** A horizontal bar chart comparing retention percentages across platforms. Paired with the Views chart to show the views-vs-quality tradeoff.

Charts use the channel's primary brand color from channel_profile_settings. They are generated server-side or via lightweight JavaScript (Chart.js or similar). They render correctly without JavaScript via a fallback table.

### Verdict block

Below the charts, a structured summary block written in plain language:
- One line per dimension: Best platform for views, engagement, retention, revenue.
- One recommendation sentence summarizing the platform strategy implication.
- Source note: "Based on data available for this episode."

The recommendation is rule-based (not AI-generated) at P0. Highest views = "TikTok drove the most views." Highest retention = "YouTube had stronger retention." The implication is a preprogrammed decision tree. AI-generated cross-episode platform strategy is a P1/Learning Module feature.

---

## 8. Revenue Tab

The Revenue tab makes the factory's economics visible at the episode level.

### Layout overview

```
REVENUE  ────────────────────────────────────────────────────────────────────

  ECONOMICS SUMMARY

  ┌─────────────────────────────────────────────────────────────────────┐
  │  Production Cost   Platform Revenue   Sponsor Revenue   Net Result  │
  │    $11.80              $3.40               $0.00          -$8.40    │
  │   (total AI cost)    (YouTube)          (not entered)    (loss)     │
  └─────────────────────────────────────────────────────────────────────┘

  ─────────────────────────────────────────────────────────────────────────

  PRODUCTION COST BREAKDOWN

  Voice synthesis         $0.38    3.2%   ██████░░░░░░
  Image generation        $1.20   10.2%   ████████████████░░░
  Video generation        $8.90   75.4%   ████████████████████████████████
  LLM (script/research)  $0.76    6.4%   ████████░░░░
  Assembly / render       $0.38    3.2%   ██████░░░░░░
  Other                   $0.18    1.5%   ███░░░░░░░░░
  ─────────────────────────────────────────────────────────
  Total                   $11.80

  ─────────────────────────────────────────────────────────────────────────

  REVENUE

  YouTube AdSense        $3.40       [Imported Jun 16]
  TikTok Creator Fund    —           [No data — Import →]
  Sponsor revenue        $0.00       [Add sponsor revenue +]
  Affiliate revenue      $0.00       [Add affiliate revenue +]
  Manual revenue         $0.00       [Add manual revenue +]
  ─────────────────────────────────────────────────────────
  Total revenue          $3.40

  ─────────────────────────────────────────────────────────────────────────

  PROFITABILITY

  Net result:     -$8.40  (loss)
  ROI:            -71%
  Break-even:     This episode needs $8.40 more in revenue to break even.

  At current YouTube CPM ($7.07):  needs 1,188 more views to break even.
  At current run rate:  estimated 8–12 weeks to break even on this episode.

  ─────────────────────────────────────────────────────────────────────────

  CHARTS

  Cost breakdown pie chart
  Revenue vs Cost comparison bar
```

### Production cost breakdown

The cost breakdown is pulled from the `costs` table, grouped by `call_type` and `provider`, then mapped to operator-readable categories:

| Internal categories | Display label |
|---|---|
| elevenlabs + tts, narration | Voice synthesis |
| muapi/seedance/higgsfield + image_generation | Image generation |
| muapi/seedance/higgsfield + video_generation | Video generation |
| openrouter/anthropic/openai + completion | LLM (script/research) |
| ffmpeg/local + assembly | Assembly / render |
| everything else | Other |

Each category shows: label, dollar amount, percentage of total, a progress bar visualizing the proportion. The highest cost category (usually video generation) is visually dominant — this is intentional, it shows the operator where their money is going.

### Revenue entry

Revenue fields that have no data show a "—" with an inline action link: "[Import →]" for platform revenue, "[Add... +]" for manual entries.

Clicking "[Add sponsor revenue +]" expands an inline form:

```
  Sponsor revenue        $0.00       [▼ Add]

  ─ Sponsor revenue ───────────────────────────────────────────────────────
  Sponsor name:    [________________]
  Amount (USD):    [$_____]
  Notes:           [________________]    (optional)
  [Save]  [Cancel]
  ─────────────────────────────────────────────────────────────────────────
```

This is a POST to an endpoint that writes to the episode record, not to platform_analytics. Sponsor revenue is episode-level, not platform-level.

### Profitability section

Three lines:
1. Net result in dollars, labeled as "profit" or "loss"
2. ROI percentage: (revenue - cost) / cost × 100
3. Break-even context: how much more revenue is needed, and what that looks like in concrete terms (views needed at current CPM, or weeks at current trajectory)

The CPM used for projection is pulled from the most recent platform_analytics records for this episode if available, or the channel's 30-day average CPM otherwise.

### Charts

**Cost breakdown pie chart:** Shows the proportion of voice, image, video, LLM, assembly costs visually. The chart uses the channel's accent color palette.

**Revenue vs. Cost bar chart:** Two bars side by side — Production Cost and Total Revenue — making the gap visible at a glance. The gap between the bars is labeled with the net loss/gain.

### Not-monetized state

When `channel_profile_settings.monetization_status = 'not_monetized'`, the Revenue section shows:

```
  REVENUE

  This channel is not yet monetized.
  Platform revenue will appear here once YouTube Partner Program is active.

  [Update monetization status]
```

Sponsor, affiliate, and manual revenue fields remain available even when not monetized.

---

## 9. Learning Tab

The Learning tab is the most important section. It answers the question every operator asks after a video goes live: "What did I learn from this?"

### Layout overview

```
LEARNING  ─────────────────────────────────────────────────────────────────

  WHAT THE FACTORY LEARNED

  This episode generated 4 learning signals.
  3 are actionable. 1 has been applied.

  ─────────────────────────────────────────────────────────────────────────

  ✦ THUMBNAIL — HIGH CONFIDENCE
  ─────────────────────────────────────────────────────────────────────────
  What happened:
  The dark-background close-up thumbnail achieved 5.2% CTR — matching
  your channel average. This is consistent with your 3 previous episodes
  using the same composition style.

  What it means:
  Dark background + artifact close-up is your baseline style. It performs
  reliably but does not outperform. To beat your average, experiment with
  a face-present thumbnail or a high-contrast split-panel composition.

  Recommendation for next episode:
  Test a face-present thumbnail alongside your standard style to compare.

  Status: ○ Pending                    [Apply to next episode]  [Dismiss]

  ─────────────────────────────────────────────────────────────────────────

  ✦ TITLE — MEDIUM CONFIDENCE
  ─────────────────────────────────────────────────────────────────────────
  What happened:
  The published title (Statement format) achieved 5.2% CTR — on baseline.
  Your question-format titles have averaged 6.1% CTR across 5 episodes.

  What it means:
  This episode used a Statement title when a Question title was available
  as an alternative. The available question title was: "Did Ancient Greeks
  Build a Computer? The Antikythera Secret."

  Recommendation for next episode:
  Prioritize question-format titles for the next 3 episodes to build a
  stronger pattern signal.

  Status: ○ Pending                    [Apply to next episode]  [Dismiss]

  ─────────────────────────────────────────────────────────────────────────

  ✦ PLATFORM — MEDIUM CONFIDENCE
  ─────────────────────────────────────────────────────────────────────────
  What happened:
  TikTok generated 12,400 views vs YouTube's 4,821 for this episode.
  However, YouTube retention (62%) significantly exceeded TikTok (34%).

  What it means:
  This content type (ancient historical artifacts) attracts more initial
  viewers on TikTok but retains them better on YouTube. TikTok appears
  to be a discovery channel while YouTube is the engagement channel.

  Recommendation for next episode:
  Optimize the TikTok cut for a stronger hook in the first 3 seconds.
  Continue prioritizing YouTube for long-form retention.

  Status: ✓ Applied (Jun 18)           [View applied change]

  ─────────────────────────────────────────────────────────────────────────

  ✦ TOPIC — LOW CONFIDENCE (not yet actionable)
  ─────────────────────────────────────────────────────────────────────────
  What happened:
  This is the first "ancient technology" episode. Performance data exists
  but there is no prior episode in this topic cluster to compare against.

  What it means:
  Not enough data yet. This signal will become actionable after 3+ episodes
  in the same topic area.

  Status: ○ Waiting for more data      (not actionable yet)

  ─────────────────────────────────────────────────────────────────────────

  APPLIED TO NEXT EPISODE

  The following learnings from this episode have been applied to the
  next episode in the queue: "The Oracle at Delphi"

  ✓ Platform recommendation: TikTok hook optimization  (applied Jun 18)
  ○ Thumbnail recommendation: Face-present test        (pending approval)
  ○ Title recommendation: Question-format priority     (pending approval)

  [View next episode →]   [View all channel learnings →]
```

### Learning card structure

Each learning is a named card with five sections:

1. **Header:** Dimension (Thumbnail / Title / Platform / Audience / Topic / Cost) + Confidence level (High / Medium / Low). Confidence is displayed as a label, not a number. High = enough evidence for a clear pattern. Medium = directional signal but not conclusive. Low = early signal, not yet actionable.

2. **What happened:** Factual description of what the analytics showed for this episode. No jargon. Numbers in plain language. "The dark-background thumbnail achieved 5.2% CTR" — not "signal_strength: -0.02, affected_dim: thumbnail."

3. **What it means:** Interpretive layer. What does this performance result tell us about this channel's patterns? Connects the episode-level data to the channel-level trend.

4. **Recommendation for next episode:** One clear action in plain language. Always phrased as something the operator can decide to do or not do. Not a mandate.

5. **Status bar:** The apply/dismiss controls, or the current status if already acted on.

### Confidence level definitions

**High confidence:** At least 5 episodes in the same dimension with consistent signal. "Your last 5 question-format titles averaged 6.3% CTR with low variance."

**Medium confidence:** 2–4 episodes with a directional pattern. Signal is present but could change with more data.

**Low confidence / Waiting:** 0–1 comparable episodes, or conflicting signals. Learning is shown for transparency but flagged as not yet actionable. No Apply button.

### Apply / Dismiss behavior

**Apply:** Marks the learning action as applied and adds a note to the next queued episode's context record. The pipeline will incorporate this signal when generating the next episode. The note is human-readable, not a schema flag: "Apply question-format title priority for next episode, based on learning from ep_003."

**Dismiss:** Marks the learning action as dismissed with an optional reason: "I intentionally used a Statement title" or "Not applicable to next episode." Dismissed actions are archived and do not re-appear for future episodes in the same cluster unless explicitly requested.

### "Applied to next episode" section

A summary at the bottom of the Learning tab showing which learnings from this episode have already been linked to the next episode in the queue. This creates a visible chain: "What we learned from ep_003 → what changes we made for ep_004." The operator can see that learning is actually flowing forward, not accumulating in a database that nothing reads.

---

## 10. Branding Tab

The Branding tab shows the visual identity elements applied to this specific episode and manages the channel's permanent branding configuration.

### Layout overview

```
BRANDING  ──────────────────────────────────────────────────────────────────

  LOGO BUG

  ┌──────────────────────────────────────────────────────────────────────┐
  │                                                                      │
  │           [Video preview frame — 560 × 315px]                       │
  │                                                                      │
  │  [Logo]                                                              │
  │                                                                      │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘

  Position: Bottom-left    Opacity: 80%    Size: Small (5% frame width)
  Safe zone: ✓ Validated   Subtitle area: ✓ Clear

  [Edit logo position]   [Preview on full video]

  ─────────────────────────────────────────────────────────────────────────

  HASHTAG OVERLAY

  Generated hashtags:
  #AncientGreece  #Antikythera  #AncientTechnology  #HistoryDocumentary
  #AncientMysteries  #GreekHistory  #LostTechnology

  Overlay position: Bottom-center during intro (first 5 seconds)
  Overlay style: White text on dark background, fade-in/out

  ┌──────────────────────────────────────────────────────────────────────┐
  │  [Intro frame preview with hashtag overlay]                          │
  │                                                                      │
  │  #AncientGreece  #Antikythera  #AncientTechnology                   │
  └──────────────────────────────────────────────────────────────────────┘

  [Edit hashtags]   [Regenerate hashtags]   [Preview on video]

  ─────────────────────────────────────────────────────────────────────────

  PLATFORM-SPECIFIC VARIANTS

  YouTube:   Full video with logo bug + hashtag overlay burned in
  TikTok:    Full video with logo bug. Hashtags in description, not overlay.
  Instagram: Square crop (1:1) version with logo bug. Hashtags in caption.
  Facebook:  Same as YouTube variant.

  [Preview YouTube version]   [Preview TikTok version]

  ─────────────────────────────────────────────────────────────────────────

  RENDER VERSION

  Render: v1 (Jun 9, 14:45)   Logo: hypogeum-logo-v2.png   Safe zone: Passed
  [Re-render with updated branding →]
```

### Logo bug section

**Preview frame:** A still frame from the episode video (the first frame, or a representative middle frame) at 560×315px with the logo bug overlaid in its actual position. This is a static image composite — it does not require video rendering.

**Position controls:** The position is set in Channel Profile and applied to all episodes. This tab shows the result for this specific episode. If the operator changes the position in Channel Profile, a note appears: "Position updated in Channel Profile. Re-render this episode to apply new position."

**Safe zone validation:** Two indicators:
- "Safe zone: ✓ Validated" — logo does not overlap platform UI elements (YouTube subscribe button position, TikTok bottom UI area)
- "Subtitle area: ✓ Clear" — logo does not overlap the lower-third subtitle region

If either fails, show: "⚠ Logo overlaps TikTok UI area — [Adjust position]."

**Opacity and size** are channel-level settings shown here for reference.

### Hashtag overlay section

**Generated hashtags:** The full list of hashtags generated for this episode. Displayed as tags. The operator can click any hashtag to remove it, or type to add a new one inline.

**Regenerate:** Triggers a new hashtag generation from the episode script and topic. Cost: minimal (LLM call).

**Preview:** Shows a static frame composite with the hashtag overlay applied. Same render-free approach as the logo preview.

### Platform-specific variants

Different platforms have different aspect ratios, safe zones, and overlay conventions. This section lists the planned variant for each enabled platform:

- YouTube: 16:9, full video, logo + overlay burned in
- TikTok: 9:16 (vertical crop or adapted edit), logo bug, hashtags in description field not video overlay
- Instagram: 1:1 or 9:16, logo bug, hashtags in caption
- Facebook: 16:9, same as YouTube variant

For P0, this section is informational — it shows what will be done, not what has been done. Platform-specific render variants are a P1 feature.

### Render version

Shows which version of the video was rendered with which branding assets. When Channel Profile branding changes (new logo, new color), a notification appears: "Channel branding was updated after this episode was rendered. Re-render to apply new branding."

---

## 11. UI Requirements

The Episode Detail page must feel like a professional product, not a debug console. The reference quality level is: **Linear**, **Vercel dashboard**, **Stripe Dashboard**, **YouTube Studio**.

### Visual design principles

**Dark mode by default.** The existing base style is dark — maintain it. The Episode Detail page uses the same `--bg`, `--surface`, `--border`, `--text`, `--muted` CSS variables already defined in `_base_style.html`. No new color system is introduced.

**Cards, not tables.** The current product overuses tables for everything. The Episode Detail page uses cards for content-heavy sections (thumbnails, learning) and tables only where structured comparison is genuinely needed (platform analytics comparison, cost breakdown).

**Tabs, not endless scroll.** The page uses a tab navigation structure: Production · Publishing · Thumbnails · Titles · Analytics · Revenue · Learning · Branding. The header is always visible. The active tab's content fills the space below.

**No raw IDs in the operator view.** Episode IDs, run IDs, asset IDs, and model slugs do not appear as primary content. They are hidden in expandable "Advanced" sections or shown only as subtle chips that can be dismissed. The exception is Platform Video IDs — these are shown because the operator may need to share them or look them up on the platform.

**No engineering jargon.** All labels are operator language. A full jargon-to-operator mapping:

| Internal label | Display label |
|---|---|
| stage_exec_id | Stage run |
| gate_verdict | Quality check result |
| BGA Pass 1 / BGA Pass 2 | Brand compliance check |
| signal_strength | Performance signal |
| affected_dim | Content area |
| source='mock' | Demo data |
| run_id | Production run |
| asset_type | Asset type |
| completion_rate | Watch completion |
| avg_view_pct | Average watch percentage |
| metadata_version | (hidden — never shown) |

**Status badges use language, not code.** Pipeline status `complete` → "Published." `in_progress` → "In production." `failed` → "Failed." Internal status values are never shown raw.

### Tab navigation

```
[Production]  [Publishing]  [Thumbnails]  [Titles]  [Analytics]  [Revenue]  [Learning]  [Branding]
 ──────────────────────────────────────────────────────────────────────────────────────────────────
```

Active tab: underline or background highlight. Tabs with pending actions or alerts show a badge count: `[Learning ⚡3]` for 3 pending learning actions.

### Charts

Charts must render without external CDN dependencies. Use Chart.js (already commonly available) or a lightweight SVG-based chart. Charts fall back to a data table when JavaScript is unavailable.

Chart colors: use `--blue` for production data, `--green` for revenue/positive, `--red` for cost/negative, `--yellow` for warnings. These align with the existing color system.

### Responsive behavior

- Desktop (>1024px): Full side-by-side layouts for header KPIs, platform comparison, thumbnail candidates.
- Tablet (768–1024px): Header KPI cards wrap to two rows. Platform cards stack to 2-column grid.
- Mobile (<768px): All cards stack single column. Tab bar becomes horizontally scrollable. Charts reduce to minimum 300px width.

### Empty state design

Every tab must have a designed empty state when data is not yet available:

```
  THUMBNAILS

  No thumbnail candidates yet.

  This episode may still be in production, or thumbnails
  may not have been generated for this run.

  [View production status →]
```

Empty states explain why data is missing and offer a next action. They never just show "No data" or a blank area.

### Loading states

When the page is first loaded and data is being fetched, skeleton loading cards appear in place of content: grey animated blocks in the shape of the cards. This is better than a spinner and better than blank white space. Loading should complete within 1 second for any reasonable database size.

---

## 12. Navigation Refactor

After the Episode Detail page exists, many existing navigation items lose their purpose as primary destinations.

### A) Pages that stay visible in the primary nav

**Dashboard (`/dashboard`):**  
The home page. Now routes episode rows to `/episode/<episode_id>`. Stays as the landing page.

**Episodes index (`/episodes` → renamed to `/content`):**  
Renamed from "Episodes" to "Content." Shows a visual grid of episodes with thumbnails, published titles, status badges, and CTR. Clicking any episode opens the Episode Detail page. The "Enqueue Topic" form moves out of this page to a separate "New Episode" flow accessible from the dashboard "Start production" button.

**Analytics portfolio (`/analytics`):**  
Stays visible for channel-level cross-episode performance. Renamed from "Analytics" to "Performance." Per-episode analytics routes redirect to the Episode Detail Analytics tab.

**Queue (`/queue`):**  
Stays visible. This is where the operator manages the topic backlog and production schedule. Simplified to show: queued topics, claimed topics, production schedule.

**Channel (`/channel-profile` → `/channel`):**  
Renamed from "Channel Profile" to "Channel." Expanded to include the full channel identity from the Channel Profile spec, including logo upload, monetization status, goals, and publishing targets.

**Signals summary (renamed `/warnings`):**  
Replaces the current "Signals" nav item. Shows only the operator-facing warnings: credential health, content cadence health, performance anomalies. Hides OIS internals. Linked from the dashboard warnings bar.

### B) Pages that move into Operations (hidden from primary nav)

Operations is a collapsible section at the bottom of the nav or a separate `/ops` sub-navigation. These pages remain fully functional but are not surfaced to operators by default.

- `/run/<run_id>` — Run detail (technical audit log)
- `/runs` — All runs list
- `/signals` — Raw OIS signals
- `/decision-inputs` — Decision engine readiness
- `/decisions` — Decision engine output
- `/decision-reviews` — Decision review queue
- `/execution-plans` — Execution plans
- `/preflight` — Brand compliance audit
- `/approvals` — All approvals
- `/failures` — Failure logs
- `/live` — Live run stream
- `/command` — Factory command center (the valuable parts of this page — Launch Readiness, Needs Attention — are promoted to the dashboard directly)

### C) Pages that disappear entirely

- `/analytics/episode/<episode_id>` — Redirects to `/episode/<episode_id>?tab=analytics`. No standalone page.
- `/analytics/platform/<platform>` — Merged into the Analytics portfolio page as a tab, not a separate route.
- `/learning` — The episode-level learning content moves to the Episode Detail Learning tab. The channel-level learning summary card is added to the main dashboard. The `/learning` route remains as a global intelligence overview but is no longer in the primary nav. It becomes an "Advanced" link from the main dashboard learning card.

### New nav structure (7 items)

```
Factory OS | Content | Performance | Queue | Channel | Warnings | [+New Episode]
```

The final item `[+New Episode]` is a primary action button (blue, right-aligned) that opens the topic queue and starts a new production run. This replaces the buried `/episodes/enqueue` form.

---

## 13. Data Requirements

All data fields the Episode Detail page requires, grouped by tab. This defines what must be retrievable — not how the schema stores it.

### Production

- Episode ID
- Internal topic brief (as given to the pipeline)
- Production run start time
- Production run completion time
- Total production duration (elapsed)
- Current status (in_progress / complete / failed)
- For each stage: name, status, started_at, completed_at, duration, cost, error message, artifacts produced (count and type), model used
- Final video file path
- Script text content
- Audio/voiceover file path
- Audio duration
- Gate verdicts: gate name, verdict (passed/failed/warning), flag count, blocking count
- Error message if failed (translated to operator language)
- Total production cost
- Cost by call type / provider

### Publishing

- Per platform: publish status, platform video ID, direct URL, published_at timestamp, upload duration, upload error message, last analytics sync timestamp

### Thumbnails

- All thumbnail candidates for this episode: candidate ID, rank, image file path, text overlay, placement, visual prompt, model used, seed, generation cost, generation date, selected boolean
- For the selected thumbnail: CTR, impressions, views (from platform_analytics, YouTube only for P0), performance band vs. channel average

### Titles

- All title candidates for this episode: title text, selected boolean, created_at, generation source
- For the selected title: CTR, impressions, views (from platform_analytics), performance band vs. channel average
- Channel title pattern history: for each title format type, count of episodes and average CTR

### Analytics

- Per platform: views, impressions, CTR, avg_view_pct, avg_watch_time_s, total_watch_time_s (or computed), likes, comments, shares, saves, subscriber_gain, revenue_usd, snapshot_date, source (real/manual/mock)
- Data freshness: last_imported timestamp per platform
- Analytics insights for this episode: affected dimension, insight type, signal strength, detail text
- Channel-level 30-day average CTR and retention for comparison

### Revenue

- Total production cost (from costs table)
- Cost by category (voice, image, video, LLM, assembly, other — mapped from provider + call_type)
- Platform revenue per platform (from platform_analytics.revenue_usd — not yet in schema, noted as required)
- Sponsor revenue (manual entry — not yet in schema, noted as required)
- Affiliate revenue (manual entry — not yet in schema, noted as required)
- Manual/other revenue (manual entry — not yet in schema, noted as required)
- Channel CPM 30-day average (for break-even projection)
- Monetization status from channel profile

### Branding

- Channel logo file path and version
- Logo position setting (top-left / top-right / bottom-left / bottom-right)
- Logo opacity setting
- Logo size setting
- Logo safe zone validation result for this episode
- Generated hashtags for this episode
- Hashtag overlay position setting
- Hashtag overlay style setting
- Video render version number and date
- Per-platform variant status (rendered / not rendered)

### Learning

- Per-episode learning actions from learning_actions: action_id, dimension, action_type, rationale, status (pending/applied/dismissed), source_episode
- Per-episode analytics insights from analytics_insights: insight_type, affected_dim, signal_strength, detail, platform
- Application log: which learnings were applied to which subsequent episode
- Channel-level learning context: minimum evidence threshold met per dimension, recommendation confidence level per dimension

---

## 14. P0 Build Order

The goal: launch the first video entirely from the dashboard, then run the factory automatically for 30 days, then stabilize, then migrate to Mac mini, then V2.

### Build order — strict sequence

**Step 1 — Episode Detail page (static, no analytics)**

Build `/episode/<episode_id>` with the header and the Production tab only. No analytics, no thumbnails, no titles yet. This alone replaces the need to navigate to `/run/<run_id>` for episode status.

Wire the episodes list to link to this page instead of `/run/<run_id>`.

Wire the dashboard Recent Episodes table to link to this page.

Acceptance: operator can open any episode and see production status, stage timeline in operator language, and cost — without seeing a run ID or a gate verdict code.

**Step 2 — Publishing tab**

Add the Publishing tab to the Episode Detail page. Show per-platform status cards with publish date, platform URL, and last sync. Add the "Re-publish" quick action to the header.

Wire `/published/import` to link back to the Episode Detail Publishing tab after import.

Acceptance: operator can see where a video was published and open it on the platform, without navigating away from the episode.

**Step 3 — Thumbnail tab (display only)**

Add the Thumbnail tab. Display the three candidate images (if image files exist on disk) with their prompts, models, and generation costs. Mark the selected thumbnail. Performance fields show "—" until analytics are imported.

This step requires: `thumbnail_candidates.image_file_path` must exist as a column or the image must be findable via `assets` table by matching episode_id and asset_type.

Acceptance: operator can see the three thumbnail options that were generated, side by side, with their prompts.

**Step 4 — Title tab (display only)**

Add the Title tab. Display all title candidates from the `titles` table. Mark the selected title. Performance fields show "—" until analytics are imported.

Acceptance: operator can see all title options generated, not just the one that was published.

**Step 5 — Dashboard launch button**

Add the "Start production" button to the main dashboard. This opens a modal or a simple form: "New Episode — enter topic — [Start]." The form POSTs to `/episodes/enqueue` and redirects to `/episode/<episode_id>` once the run is created and the first stage begins.

Wire the live production panel on the dashboard to link to `/episode/<episode_id>` (not `/run/<run_id>`).

Acceptance: operator can queue an episode from the dashboard, see it start production, and navigate to the Episode Detail page to watch it progress — without touching the terminal.

**Step 6 — Analytics tab (basic)**

Add the Analytics tab. Pull from `platform_analytics` for this episode. Show the cross-platform comparison table. No charts yet — tables only. Show data source indicator (real / mock / no data).

Wire `/analytics/import` so that after import, the user is redirected to `/episode/<episode_id>?tab=analytics`.

Acceptance: after importing analytics, the operator sees the performance data on the Episode Detail page, not on a separate `/analytics/episode/<episode_id>` page.

**Step 7 — Revenue tab (cost only)**

Add the Revenue tab. Show the production cost breakdown (from `costs` table, grouped by provider/call_type). Revenue fields show "—" with "[Add revenue]" inline forms for manual entry.

Acceptance: operator can see exactly where their production money was spent, broken down by cost category.

**Step 8 — Learning tab (actions from this episode)**

Add the Learning tab. Surface the `learning_actions` records where `source_episode = episode_id` and the `analytics_insights` records for this episode. Display them as operator-language learning cards (not the current dimension-badge + signal-strength format from `/analytics/episode/`).

Acceptance: operator sees what the factory learned from this episode in plain language, with an Apply button they can use to confirm the learning for the next episode.

**Step 9 — Navigation refactor**

Rename nav items per Section 12. Move developer pages to an Operations section. Add the `[+New Episode]` primary action button to the nav.

Update the dashboard to show the last published episode card with thumbnail image, published title, and a link to the Episode Detail page.

Acceptance: the primary nav has 7 items. The operator can navigate the entire product without encountering the term "OIS," "gate verdict," "preflight," "signal," or a raw UUID as primary content.

**Step 10 — First video from the dashboard**

Run a complete episode from the "New Episode" button on the dashboard to publication on YouTube. Monitor via the Episode Detail page. Import analytics. Review learning actions. This is the P0 milestone.

If anything fails: fix it. If the operator has to open a terminal for any step: identify why and fix it before declaring P0 complete.

**Step 11 — 30-day automation run**

Activate the topic queue with 30 pre-written topics. Set the scheduler to one episode per week. Let it run. Review the Episode Detail page for each episode as it completes. Import analytics after each publish. Apply learning actions from the Learning tab.

During this month: identify what information the operator actually needs that isn't on the Episode Detail page. Note every time the operator has to leave the dashboard to find something. These notes become the P1 backlog.

**Step 12 — Stabilization and Mac mini migration**

Fix all issues identified during the 30-day run. Ensure the dashboard is accessible from any device on the local network. Set up the factory as a service that survives restarts. The Mac mini migration is complete when: the factory runs an episode without the development machine being involved, and the operator can review the result on the Episode Detail page from any device.

**Step 13 — V2 scope begins**

Only after Step 12 is confirmed stable does V2 design begin. V2 adds: multiple channels, access control, revenue intelligence at the channel level, campaign intelligence, white-label support, SaaS/private cloud deployment. V2 is designed based on what was learned from the first six months of running a single channel — not based on schema speculation.

---

*EPISODE_DETAIL_PRODUCT_SPEC.md — 2026-06-19*  
*Product design only. No code. No schema changes. No migrations. No tests.*
