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

Phase 2 complete (exit criteria met); Phase 3 (Production Hardening) is
now active — see `CURRENT_PHASE.md`

Roadmap Progress

Foundation → Largely Complete

Autonomous Production → Operational, validated end-to-end

Autonomous Runtime → Core objectives met, monitoring and recovery hardening in progress

Analytics → Partial

Learning → Early Stage

Multi-Channel → Planned

Commercial Platform → Planned

Documentation → Synchronized with implementation as of 2026-07-25 (Cortex
Constitution adopted, Architecture Alignment Report reviewed, ADR-008
recorded for Service Layer convergence). Documentation work is closed
until implementation materially changes reality.

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

- Multi-channel execution
- Organization management
- White-label deployment
- Marketplace
- Public SDK
- Public API
- Billing
- Licensing

---

# Current Priorities

The current implementation priorities are:

1. Complete Phase 3 (Production Hardening) P0, one capability at a time,
   in order: Log Rotation, Recovery System, Runtime Observability,
   Recovery & Self-Healing, Startup & Shutdown Verification. See
   `CURRENT_PHASE.md` and `CURRENT_PROGRESS.md` for full scope and live
   status — this supersedes the former "strengthen runtime monitoring
   and recovery" priority, which is now this item's P0.3/P0.4 in detail
   rather than a single line here.

2. Continue burn-in observation until sustained (not single-instance)
   autonomous reliability is demonstrated (5–10 unattended runs per
   `ROADMAP.md`'s Release Candidate Criteria).

3. Complete multi-platform publishing: Facebook verification (paused
   mid-task, resume after burn-in priority clears), then resolve the two
   external blockers (Instagram staging URL, Facebook/TikTok scope
   re-authorization) — operator-owned, not code work.

4. Complete analytics feedback loops. — Blocked until priority 1 (Phase 3
   P0) is complete, per `ROADMAP.md`'s Phase 4 gating note.

5. Expand learning systems. — Blocked until priority 1 (Phase 3 P0) is
   complete, per `ROADMAP.md`'s Phase 5 gating note.

No new strategic feature work should bypass these priorities unless it directly removes a blocker.

Superseded: "Complete reliable autonomous runtime" and "Achieve
unattended end-to-end production" (the two former top priorities) are
substantially achieved — see Autonomous Runtime above — and are folded
into priority 1's sustained-reliability bar rather than listed as
separate open items.

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