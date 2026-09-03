# Dashboard → paid-product polish (tracker)

**Goal (operator's words, 2026-09-03):** the dashboard must look and feel
like a product people pay to own — visual polish, animation, full
consistency, zero bugs, nothing off-target. Analytics and learning
rendered as attractive cards and hero sections.

Design system of record: `design-system/factory-os/MASTER.md` (+
`pages/*.md` overrides). Two-layer skin: dark base for daily ops,
`:root[data-skin="presentation"]` overlay for client demos. Ember accent
`#f0a838`; green = semantic "good" only. System fonts (offline; no CDN).
All styling in `hypogeum/templates/_base_style.html` (shared) — never
per-page `<style>` unless truly page-local.

## Cross-cutting issues found in the survey

1. **No real hero sections** — pages open with a bare `h1 + count`. Need a
   shared hero: eyebrow · headline · one-line lede · primary action · an
   at-a-glance stat or status.
2. **Weak / broken-looking data viz** — 132px sparklines floating in
   half-empty cards; `—` and `0%` everywhere read as broken rather than
   "no data yet".
3. **Raw machine data leaking to the operator** — ISO timestamps
   (`2026-09-03T08:00:00+00:00`), channel slugs (`hypogeum`), `priority 9`,
   `2026-W36`. Needs humanising helpers (relative/local time, friendly
   names).
4. **Flat cards** — Learning insights are plain rows; signal map is a bare
   table; platform-coverage is bare numbers.
5. **Minimal motion** — only a 4px body fade. Presentation skin has a
   stagger but few pages opt in (`.skin-stagger`).
6. **Consistency** — Overview has a hero-ish block; almost nothing else
   does. Card variants, empty states, and section headers differ per page.
7. **Machine language shown to a non-technical owner** (operator, 2026-09-03):
   - Episode identity shows as `ep947837` / `ep_f38dc597` instead of the
     real title ("The Real Atlantis Was Found in Greece"). Never show a
     raw episode id where a human title exists.
   - Problems/blockers show as codes — `A_VIDEO`, `blocked_metadata`,
     `F_THUMBNAIL`, `3 consecutive failed runs (last: other)`. A paying
     client cannot read these. Every blocker/error surfaced in the UI
     needs a plain-language label + one plain sentence of "what to do".

## Shared work (do first — every page inherits it)

- [ ] `_hero.html` partial + `.hero` / `.hero-*` tokens in `_base_style.html`
- [ ] `.stat-card` / `.stat-grid` richer KPI card (trend, delta, sparkline slot)
- [ ] `.dataviz-empty` intentional "no data yet" treatment (replaces bare `—`)
- [ ] `humanize` Jinja filters: `local_dt`, `rel_time`, `friendly_channel`
- [ ] opt every refreshed page's top grid into `.skin-stagger`
- [ ] chart helpers in `_charts.html`: bigger area chart, bar chart, donut

## Page passes (depth-first on demo-critical, then breadth)

| # | Page | Template | State | Notes |
|---|------|----------|-------|-------|
| 1 | Learning / Intelligence | `learning.html` | ✅ done | hero, insight cards, diverging signal bars, full plain-language |
| 2 | Analytics / Performance | `analytics.html` | ✅ done | hero, stat-grid, full-width areachart, platform cards, titles not ids |
| 3 | Overview / Control room | `exec_dashboard.html` | ✅ pass 1 | Recent/Latest Episodes humanised, dropped Platform-ID col + debug footer |
| 4 | Attention / Review | `attention.html` | ✅ done | hero, plain learning items, icons not emoji |
| 5 | Channels | `channels.html` | ✅ done | plain runtime block, friendly_week, plain_channel_blocker |
| 6 | Episodes (queue) | `episodes.html` | ✅ done | hero, channel names, plain queue meta; fixed _is_test_episode |
| 7 | Production (`/create`) | `create.html` | ✅ done | hero, plain queue rows, plain recovery panel |
| 8 | Episode detail | `episode_detail.html` | ✅ pass 1 | header + steps + QA humanised; **Publishing tab Zernio-aware**; thumbnails empty state |
| 9 | Channel detail | `channel_detail.html` | ✅ done | hero, stat-grid, readiness w/ SVG marks, learning insight cards |
| 10 | channel readiness data | `channel_readiness.py` | ✅ done | `_plainify()` scrubs worker/OIS/POST-api jargon from every item |
| 11 | Runs → "Production history" | `runs.html` | ✅ done | hero, titles not run-ids, fixed the broken Duration column |
| 12 | Run detail | `run_detail.html` | ✅ done | plain stage names (`stage_name` filter), plain_problem errors |
| 13 | Discovery | `discover.html` | ✅ done | "Topic ideas for <channel>", dropped the 8-dimension engineer block |
| 14 | Channel Publishing | `channel_publishing.html` | ✅ done | hero, friendly_platform, no `/platforms` OAuth link |
| 15 | Live view | `live.html` | ✅ done | "What's happening now"; dropped Tokens; plain stage names in the 3s JS |
| 16 | Analytics Sync Health | `analytics_sync.html` | ✅ done | hero; renamed the colliding `.stat-*` classes; friendly platforms/times |
| 17 | Failures → "What went wrong" | `failures.html` | ✅ done | insight cards via plain_problem, traceback in a drawer |
| 18 | Asset archive | `archive.html` | ✅ done | hero; renamed the colliding `.pill`; plainer columns |
| 19 | Per-episode analytics | `analytics_episode.html` | ✅ done | hero, "By platform" comparison, insight cards, plain lessons |
| 20 | Per-platform analytics | `analytics_platform.html` | ✅ done | hero, friendly_platform, "Click rate"/"Watch-through", titles |
| 21 | Channel Settings | `channel_settings.html` | ✅ done | hero, "Payment authorization", friendly_money, "Cortex default" |
| 22 | Production plan editor | `channel_operations_edit.html` | ✅ done | dropped 90 lines of divergent page-CSS; hero; plain field labels + checkbox explainers |
| 23 | Advanced | `advanced.html` | ✅ done | hero; "operational-alerts log" not "OIS signal log" |
| 24 | Governance / Decisions | `decisions.html` | ✅ done | hero; "strong evidence" not "72% confidence + priority 1"; no JSON-API footer |
| — | plain_problem free-text | `dashboard_humanize.py` | ✅ done | `_ERROR_PHRASES` — raw pipeline errors → plain label + fix, never dumped as a heading |
| — | consistency + bug sweep | remaining ~45 | todo | Providers / Infrastructure / Storage settings / decision_inputs+reviews / setup / channel_builder / model+brand+publishing edit / connect_* / command / execution_plans / preflight / approvals_history / episode_qa / the deep episode_detail tabs |

## Known follow-ups (found while working)

- Production plan editor's "Publish time" field maps to the weekly
  `publishing_schedule` — a shorts channel's DAILY release time lives in
  `deliverable_schedule_policy.main_episode.time_of_day` (set via DB this
  session) and has no form field yet.

## Episode-detail tab bugs — all fixed 2026-09-03 (session 2)

- **Publishing tab** — was "Not connected" + old OAuth flow on every
  platform; now Zernio-aware (`_zernio_publish_outcomes` + bindings).
- **Analytics tab** — same "Not published" bug + "real data only / Mock
  rows / Import analytics" jargon; now Zernio-aware + "Numbers haven't
  come in yet, Cortex pulls them automatically" plain language.
- **Branding tab** — ran the full-length brand audit on shorts → all red
  "Missing"; now `branding.not_applicable` for shorts-only channels, one
  calm panel.
- **Thumbnails tab** — "No thumbnail candidates found" → "Short videos use
  the platform auto-cover" explanation.
- **Duration** KPI relabelled "Time to make" (it's run time, not video
  length).

## Known follow-ups (found while working)

- Publishing card shows the raw Zernio "Post ID" — fine in a record
  context but could be de-emphasised.
- `/episodes` and `/create` both render the production queue (IA overlap).
- ~10 old disk-only episodes have no title anywhere → "Untitled episode".
- old long-form hypogeum episodes now show branding "not applicable"
  because the channel switched to shorts-only — acceptable edge case.

## Non-dashboard fixes prompted by the operator this session

- **SEO tags** (`canonical_metadata_agent` + `shorts_metadata`): tags/
  hashtags now lean on the channel's broad category/format/audience
  ("bedtime story", "kids bedtime cartoon", …) instead of only
  episode-specific proper nouns — a new channel with no name recognition
  is found through generic search. Applies from the next episode.
- **Competitor + trend keyword research** (`hypogeum/channel_keyword_research.py`,
  new): 3 Perplexity queries (competitor channels + the hashtags/keywords
  they actually use · highest-volume + Google-Trends-style rising terms ·
  long-tail audience search questions) → 1 Claude structuring call → cached
  in the new `channel_keyword_research` table (~$0.03/channel, ~14-day TTL,
  zero per-episode cost). `metadata_channel_context()` merges it into the
  `channel_context` both metadata agents already send; prompts now say
  "when competitor_hashtags / high_volume_search_terms / trending_terms are
  present, build the tags and hashtags directly from them". Dashboard:
  **Search & hashtag research** card on `/channel/<id>/settings` with a
  "Research now / Refresh" button (`POST …/keyword-research/refresh`, runs
  in a background thread). First run done for twinkle_mini_tales
  2026-09-03 — 18 search terms, 20 competitor hashtags, 10 trending queries.
- **Titles tab** (`episode_detail`): short-native episodes never run
  `script_engine → title_candidates`, so the tab always showed "No title
  candidates found" + a "re-run from the Script stage" instruction. Now
  `_build_titles_data(short_native=…)` returns `not_applicable` with a calm
  "one optimised title per platform" panel + the published title — same
  treatment as the Branding and Thumbnails tabs.

## Log

- 2026-09-03: survey done; tracker created.
- 2026-09-03: shared layer landed — `hypogeum/dashboard_humanize.py`
  (episode titles, plain problem catalogue, plain status, relative/friendly
  time, money, platform, learning-action + dimension words, rationale
  cleaner, signal-strength words) + ~15 Jinja filters in run_dashboard.py.
  `_base_style.html` gained `.hero` / `.stat-card` / `.dv-empty` /
  `.sec-head` / `.insight-card` / `.pill` / `.sig-*` primitives.
  `_hero.html` macro. **Learning page fully rebuilt** — hero, insight
  cards, humanised language everywhere (no ep-ids, no "avg strength:
  -0.50", no snake_case), diverging signal-by-area bars, intentional
  empty state.
- 2026-09-03 (cont.): Analytics/Performance rebuilt (hero, stat-grid, new
  `areachart` macro, per-platform stat cards, episode TITLES everywhere,
  dropped YouTube-OAuth copy). Overview pass 1. Attention/Review rebuilt.
- 2026-09-03 (session 2): Channels, Episodes queue, Production (`/create`),
  Episode detail header + **Publishing tab made Zernio-aware** (reads
  submission ledger + bindings, no more "Connect YouTube" OAuth),
  Channel detail, `channel_readiness` plainify pass, Runs → "Production
  history", Run detail (plain stage names via `stage_name` filter,
  plain_problem for errors), Discovery ("Topic ideas for <channel>",
  dropped the "rubric-based / 8 dimensions / Backlog:" engineer block),
  channel Publishing page. ~16 commits.
  New humanize helpers: `channel_display_name`, `plain_stage_name`,
  `plain_channel_blocker`, `friendly_week`, `clean_rationale`,
  `signal_strength_words`, `_zernio_publish_outcomes` (episode_detail.py).
  Next: live view, analytics sub-pages, archive, then the engineer set
  (Providers / Infrastructure / Governance / Storage).
