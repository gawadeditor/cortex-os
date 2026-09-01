# CURRENT_STATE.md

# Cortex Current State

Version: 1.0

Status: Living Document

Authority: High

---

# Purpose

This document describes the current implementation state of Cortex.

Unlike the Architecture and Roadmap documents, this file changes continuously.

Its purpose is to answer one question:

**Where are we today?**

---

# Current Development Stage

Current Phase

Phases 0–3 complete (Phase 3 P0 shipped 2026-07-27). Work since has run
ahead of the strict roadmap order toward a **sellable multi-channel
product** — see "Since the 2026-07-28 sync" below.

Roadmap Progress (as of 2026-09-01)

Foundation (P0) → Complete

Autonomous Production (P1) → Complete; multi-platform publishing now via
Zernio (YouTube/TikTok/Instagram/Facebook connected + bound for Hypogeum)

Autonomous Runtime (P2) → Core met; sustained burn-in still thin (days,
not weeks) — the open Release-Candidate item

Production Hardening (P3) → P0 complete; plus weekly budget ceiling,
ADR-016 operating grants, operator-readable signals layer

Analytics (P4) → Partial — real per-channel analytics (source='api'
only), factory + channel rollup KPIs, per-platform pages. No
recommendation/optimisation engine.

Learning (P5) → Early — `learning_engine.py` wired into the worker loop
(`_run_learning_cycle`), Factory/Channel split, cadence recommendations.
Data-starved: ~2 actions applied.

Factory Capability Evolution (P6) → Precursor only — the Video Model
Bake-off (97-model live catalogue, per-channel adoption wired to
production). No registry, no automated promote/rollback loop.

Asset Intelligence (P7) → Mostly shipped ahead of order — archive
catalog, browser, post-publish relocation to a configurable root, reuse
provenance fields.

Multi-Channel (P8) → **Live, ahead of order.** `twinkle_mini_tales` runs
as a full Channel Package (scaffolded from template, grant signed,
schedule active). Isolation mostly holds; per-channel brand identity is
a first increment in (video scene prompts); thumbnails + per-channel
brand bibles pending.

Commercial Platform (P9) → Started — "sellable to non-technical
operators" is the working directive; dashboard-native grant signing,
plain-language operator surfaces. No orgs/accounts/billing/white-label.

Ecosystem (P10) → Not started.

Documentation → Re-synced 2026-09-01 for the ~300 commits since the
2026-07-28 sync. `ROADMAP.md` phase definitions still stand; this file is
the current state snapshot.

---

# Since the 2026-07-28 sync

The following shipped after the last documentation sync and is not
reflected in older sections below:

- **Weekly production budget enforcement** — per-channel weekly paid-video
  ceiling, an authoritative pre-plan cost model, plan-bound reservations.
- **ADR-016 Channel Operating Grant** — sign-once-per-channel autonomous
  authorization, wired into the daily path; signable from the dashboard
  via a short-lived subprocess (the runtime never holds the private key).
- **Zernio publishing** — replaced direct per-platform OAuth for
  TikTok/Instagram/Facebook/YouTube; per-channel account binding.
- **Multi-channel** — Channel Package scaffolding from a template on
  channel creation; `twinkle_mini_tales` created, granted, scheduled.
- **Asset archive** — catalog + browser + configurable archive root
  (`/Volumes/Archive/Hypogeum`) + post-publish whole-tree relocation;
  both published Hypogeum episodes relocated.
- **Dashboard** — restructured around a Factory⇄Channel split (left-rail
  scope switcher, per-channel Publishing/Settings/Operating-Grant),
  operator-readable signals layer, Zernio-centric Sync Health, the Video
  Model Bake-off, Factory/Channel rollup KPIs.
- **Per-channel video model + visual identity** — a channel's bake-off
  model choice drives its production; a channel with its own BrandProfile
  gets its own video-scene brand directive.
- **cleanup_factory_state** — keep-only-published-episodes cleanup;
  superseded-run + dead-queue categories.

---

# Completed Foundations

The following platform foundations are considered operational.

## Core

- Runtime framework, including a validated Minimum Autonomous Runtime
  (launchd-supervised worker, live-tested against a hard process kill with
  full before/after evidence)
- Orchestration framework
- Multi-agent workflow
- Configuration system
- Secret management, including a single canonical runtime source of truth
  for OAuth-rotatable credentials (`secret_profiles`, `.env` as bootstrap
  fallback only)
- Provider abstraction
- Execution pipeline

---

## Production Pipeline

Operational stages include:

- Topic discovery
- Research
- Verification
- Narrative structure
- Script generation
- Script review
- Script revision
- Voice generation
- Visual planning
- Thumbnail generation
- Assembly
- Publishing pipeline

---

## Skills

The Skills framework exists.

Core production components are being migrated toward reusable Skills.

Skills are becoming the preferred mechanism for reusable expertise.

---

## Provider Layer

Provider abstraction exists.

Current integrations include multiple AI providers across text, image, video, and voice generation.

Provider selection is partially automated.

Benchmarking remains incomplete.

---

## Dashboard / Interfaces

The Dashboard (`run_dashboard.py`) is the primary operator-facing
Interface (`CORTEX_ARCHITECTURE.md` Layer 1) and is already in daily
operational use — not planned work, existing work. Per `ROADMAP.md`'s
"Interfaces (Cross-Cutting, Not a Phase)" note, it does not have its own
roadmap phase; it grows alongside whichever capability phase is active.

**Known architectural debt** (Architecture Alignment Report,
`docs/architecture/CORTEX_ALIGNMENT_REPORT.md`, Conflict #4): OAuth
token-exchange business logic currently lives directly inside
`youtube_oauth_callback()`/`meta_oauth_callback()` route handlers — a
concrete, already-identified violation of `CORTEX_ARCHITECTURE.md`'s
"Interfaces never own business logic" rule. Not fixed yet; not to be
extended further by new Dashboard work.

**Governance note**: the UI/UX Pro Max Skill requirement for Dashboard
UI/UX work is an architectural decision, not present implementation
state — recorded in `DECISION_LOG.md` (ADR-009) and `SKILLS_SPEC.md`'s
skills governance, per `GOVERNANCE.md`'s own ownership mapping ("Skills →
Skills Specification"). Not restated here; see those documents.

**Channel-agnosticism requirement**: the Dashboard must be designed and
maintained as a channel-agnostic Cortex operational product. Hypogeum
supplies Channel Package configuration and data only — the Dashboard
itself must never hardcode Hypogeum-specific assumptions, matching
`CORTEX_ARCHITECTURE.md`'s existing rule that the Core "knows nothing
about any specific channel," which Interfaces inherit.

**Pre-existing legacy design docs**: `docs/dashboard-architecture-system.md`,
`docs/dashboard-product-architecture.md`, `docs/dashboard-director-system.md`,
`docs/dashboard-surface-area.md` (~4,800 lines total, dated 2026-06-13)
constitute a substantial prior Dashboard design body of work, written
under a pre-Cortex-Constitution phase-numbering scheme ("Phase B5",
"B5.3", "B5.25", "B5.275", "B5.35"). A reconciliation pass (2026-07-26)
annotated all four with the current phase-numbering context and the
skill-prerequisite/channel-agnosticism requirements above. That pass also
surfaced a real, structural finding, not a naming-only issue: the
"Dashboard Backend API" concept running through these specs (most
concretely, "Dashboard Safety Rules" in `dashboard-product-architecture.md`
§8) describes the Dashboard's own backend directly owning business rules
— channel/run eligibility, model-swap approval enforcement, RBAC
filtering — rather than delegating to a Service. This directly conflicts
with `CORTEX_ARCHITECTURE.md` Rule 3 and ADR-004. Recorded as an extension
of Major Conflict #4 in `docs/architecture/CORTEX_ALIGNMENT_REPORT.md` —
requires an explicit decision before real Dashboard implementation
resumes, not a silent carry-forward into new work.

---

# Partially Complete Systems

The following systems exist but are not yet considered production complete.

## Autonomous Runtime

Status

Core objectives met and now validated twice under real, **unattended**
conditions — not a manually-triggered run. Burn-in (started 2026-07-25)
observed the `hypogeum_daily` schedule fire on its own on two consecutive
days with zero manual enqueue:

- **2026-07-25T15:14:49Z–15:37:32Z** (run `99391f0e`, episode
  `ep_4e35674f`): the first fully autonomous end-to-end production cycle.
  All 17 pipeline stages completed (`exit_code=0`), ending in a real,
  live YouTube publish (`video_id=8gZux3jYc_I`,
  `overall_status="published"`, `production_ready=True`). Facebook
  correctly skipped (known external blocker, see Publishing below);
  Instagram/TikTok shorts skipped (no short renders existed for this
  episode — not a failure). Cost: $2.04. This was discovered
  retroactively (2026-07-26) via direct DB/artifact inspection, not
  observed in real time, because burn-in observation was paused for
  Constitution-audit and platform-integration work in the interim — see
  `docs/operations/BURN_IN_LOG.md` for the full chronological record.
- **2026-07-26T15:15:12Z**: the second scheduled fire, observed live as
  it happened.

Checkpoint-resume (Audio/Assembly/Publishing), bounded auto-retry for
orphaned/transiently-failed queue items, and multi-platform publishing
idempotency (`publishing_outcomes` table, evolved in place with a
`deliverable_id` column) all shipped this cycle and are described in
Achieved below — these were previously listed under Remaining objectives
in this document; that listing is now stale and replaced.

Achieved

- Continuous execution (validated live via the launchd-supervised
  worker, across two independent autonomous fires)
- Background scheduling (validated live end-to-end, unattended, twice)
- Long-running worker reliability (validated against a real hard-kill)
- Checkpoint-resume for Audio, Assembly, and Publishing stages (a
  resumed run never redoes an already-verified stage)
- Bounded auto-retry for orphaned/transiently-failed queue items
  (config-driven limits/backoff via a `FailurePolicy` enum; exhaustion
  never conflates topic quality with execution reliability)
- Publish idempotency for every deliverable across all four platforms
  (YouTube main+Shorts, Facebook, Instagram Reels, TikTok) — the
  previously-documented gap ("a rerun after a crash... would re-publish")
  is closed; `publishing_outcomes` is keyed on
  `(episode_id, platform, deliverable_id)` and checked before every
  upload attempt

Remaining objectives (current priority — see Current Priorities below)

- Runtime supervision / monitoring: no proactive alerting exists yet on
  stuck runs, repeated failures, or worker health beyond what the
  dashboard surfaces on request.
- Sustained-operation evidence: only ~24 hours and two scheduled fires
  observed so far. "Cortex runs continuously" (`ROADMAP.md` Phase 2 exit
  criteria) is strongly evidenced but not yet proven over an extended
  window — burn-in observation continues.

---

## Publishing — Multi-Platform

Status

All four platforms' publishing code (OAuth, token refresh, upload,
retry, idempotency, recovery) has been verified correct via live
evidence — never assumption. YouTube is fully autonomous. TikTok,
Instagram, and Facebook are each blocked on exactly one external item
(one provider-review, one pure configuration, one operator
re-authorization of undetermined provider-review dependency) — none of
the three is a code defect.

Full detail — the exact operator checklists, verification commands,
Definition of Done per platform, and the complete readiness matrix — now
lives in `docs/operations/PLATFORM_READINESS.md` (2026-07-26), which is
the canonical, current source for this information going forward. This
section intentionally does not duplicate that detail; update the
platform-readiness document, not this one, when a platform's status
changes.

Production readiness at a glance: `youtube` unconditional. `tiktok`
conditional (Upload-to-Inbox only; Direct Post blocked on TikTok app
review). `instagram` blocked (operator configuration only — code is
ready). `facebook` blocked (operator re-authorization required; whether
provider review is also required is undetermined without performing
that live action).

---

## Analytics

Status

Partial

Current capabilities

- Publishing metrics
- Platform integrations
- Performance collection

Remaining work

- Cross-platform comparison
- KPI dashboards
- Recommendation engine
- Automated optimization

---

## Learning

Status

Early

Existing

- Initial learning structure
- Evidence collection

Remaining

- Factory learning
- Channel learning
- Prompt learning
- Provider optimization
- Thumbnail optimization
- Audience learning

---

# Planned Systems

The following major systems are intentionally deferred.

- Organization management
- White-label deployment
- Marketplace
- Public SDK
- Public API
- Billing
- Licensing

(Multi-channel execution moved out of this list 2026-09-01 — a second
channel is live. See Roadmap Progress above.)

---

# Current Priorities

Phase 3 P0 is complete. Priorities as of 2026-09-01 follow the North Star
Completion Plan (companion doc), which sequences six tracks:

1. **Channel identity (Track A)** — make per-channel brand/visual identity
   reach every generation surface, so a cloned channel is a real business
   not a re-skin. First increment shipped (video scene prompts); thumbnail
   path + per-channel brand bibles next.

2. **Sustained operation → Release Candidate (Track B)** — accumulate
   5–10 unattended runs across both channels; wire the P0.3 monitoring
   signals to a real alert channel; runbooks; queue/run janitor. DB backup
   is scheduled (launchd, 6h) and healthy.

3. **Learning loop maturity (Track C)** — the loop runs but is
   data-starved. Broaden the InsightRule set, close the prompt/thumbnail/
   topic-learning stubs. Gated on Track B accumulating episodes + analytics.

4. **Factory Capability Evolution (Track D)** — Phase 3-P1 registry
   substrate, then the automated discover→benchmark→canary→promote loop.
   Gated on Track C's Factory Learning evidence.

5. **Productization (Track E)** — start the Service-layer refactor now
   (move OAuth/business logic out of route handlers); the org/billing/
   white-label layer after A+B+C.

6. **Doc re-sync (Track F)** — this pass; then each track updates these
   docs as it lands.

The two external publishing items (Instagram staging URL, Facebook/TikTok
scope) are now moot for normal operation — publishing runs through Zernio.

---

# Known Constraints

Current development is intentionally focused on platform stability rather than feature expansion.

The objective is to establish a reliable autonomous operating platform before introducing advanced commercial capabilities.

---

# Success Criteria

The current phase is complete when Cortex can:

- Discover opportunities autonomously. — Achieved, validated live.
- Produce complete media assets. — Achieved, validated live.
- Publish without routine human intervention. — Achieved, validated live
  against the real YouTube API, on the scheduler's own unattended fire
  (not a manual trigger), on 2026-07-25 and again 2026-07-26.
- Monitor execution continuously. — Partial. No proactive alerting yet;
  observation is currently a manual/session-scoped burn-in watch, not a
  standing capability. Current priority.
- Recover from failures automatically. — Achieved for the confirmed gap
  class. Checkpoint-resume (Audio/Assembly/Publishing), bounded
  auto-retry for orphaned/transiently-failed queue items, and
  publish-idempotency across all four platforms all shipped and are
  validated (unit tests + a live TikTok rerun that correctly skipped
  re-upload). Proactive stuck-run/worker-health alerting remains open —
  see Monitor execution continuously above.
- Use collected analytics to improve future executions. — Not yet.
  Analytics feedback loop is Phase 4, not started (blocked behind Phase
  3 Production Hardening per `ROADMAP.md`).

---

# Living Document

This document should be updated whenever the implementation state changes significantly.

It reflects reality, not intention.

The Roadmap defines where Cortex is going.

This document defines where Cortex is today.

---

# Authority

This document is the official snapshot of Cortex implementation progress.

If this document conflicts with implementation,

the implementation should be corrected,

or this document should be updated to reflect reality.

Reality always takes precedence over assumptions.