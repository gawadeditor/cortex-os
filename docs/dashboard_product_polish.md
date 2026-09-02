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
| 8 | Episode detail | `episode_detail.html` | ✅ pass 1 | header + steps + QA humanised; **Publishing tab now Zernio-aware** (reads submission ledger + bindings); thumbnails empty state |
| — | consistency + bug sweep | remaining ~65 | todo | Providers/Infra/Governance/Storage + the deep episode_detail tabs (Branding audit is N/A for short-native, still shows "Missing" red) |

## Known follow-ups (found while working)

- `episode_detail` Branding tab runs the full-length branding audit on
  short-native episodes → "0 assets found / Missing verdict" in red when
  short-form genuinely needs none. Needs a short-native branch.
- Publishing card shows the raw Zernio "Post ID" — fine in a record
  context but could be de-emphasised.
- `/episodes` and `/create` both render the production queue (IA overlap).
- ~10 old disk-only episodes have no title anywhere → "Untitled episode".

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
  dropped YouTube-OAuth copy). Overview pass 1 (Recent + Latest Episodes
  humanised, "Platform ID" column + /api debug footer removed).
  Attention/Review rebuilt (hero, plain learning items, SVG icons).
  4 commits total. Next: Channels, Episodes queue, Episode detail.
