# Advisory Clients Pages Overrides

> **PROJECT:** Factory OS
> **Page Type:** Internal operator — client performance intelligence
> **Routes:** `/clients`, `/clients/new`, `/client/<id>`, `/client/<id>/content`, `/client/<id>/audience`, `/client/<id>/advice`, `/client/<id>/report`, `/client/<id>/accounts`

> ⚠️ **IMPORTANT:** Rules here **override** `design-system/factory-os/MASTER.md`.
> Only deviations are documented; everything else follows Master.

---

## Purpose

Cortex Advisory tracks content Cortex did **not** produce — shot/edited/published for
external clients whose social accounts the operator manages. These pages are a
read-mostly mirror of the Intelligence (analytics) surfaces, scoped to a `clients`
tenant instead of a producing `channels` row.

## Page-Specific Rules

### Layout

- Same `.container` + `_nav` + `_hero` shell as `analytics.html` / `monetization.html`.
- Client scope is a first-class nav scope: `?scope=client:<id>` + the "Clients" rail
  group. Client tabs: Overview / Content / Audience / Advice / Report / Accounts.
- `client_report.html` is the one client-facing page: presentation-skin first,
  a dedicated `@media print` block (hide rail/topbar, black-on-white, `@page` margins,
  `page-break-inside: avoid` per section) and a "Save as PDF / print" button that
  calls `window.print()` — no PDF library, no tokened public link.

### Color

- No overrides. Ember `#f0a838` accent, dark two-layer skin, `data-skin="presentation"` aware.
- Never imply a client's content is a Cortex production — no "episode", "pipeline",
  "render" language on these pages. It is "posts" / "content" / "published".

### Components

- KPI strip: Posts / Views / Avg engagement / Accounts.
- Best-vs-weakest content lists reuse the `analytics.html` two-column ranked pattern.
- Demographics / best-time / comments cards are copied verbatim from `monetization.html`
  so the two surfaces stay visually identical.
- Accounts page reuses the Zernio pool → link/unlink pattern; the pool is the shared
  `provider_accounts` roster (Infrastructure → Connections), not a per-client store.
- Empty states always point at the next concrete action ("Connect accounts",
  "Add your first client").

### Data source

- `hypogeum/dashboard_clients.py` — read-only aggregators, no writes from GET routes.
- Analytics rows are keyed `client_id` + `content_id` (the `content_id` doubles as the
  `platform_analytics.episode_id`), so `extract_insights` / `monetization_eligibility`
  run on client content unchanged.

---

## Anti-Patterns (Avoid)

- Leaving a linked/unlinked action with no confirmation feedback (use the `flash` line).
- Showing mock or zero metrics as if real — empty = "not synced yet", never "0".
- Mixing a client scope into factory rollups.
