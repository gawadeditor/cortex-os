# ROADMAP.md

# Cortex Development Roadmap

Version: 2.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the official implementation order of Cortex.

It does not describe architecture.

It does not describe capabilities.

It only answers one question:

What should be built next?

---

# Guiding Principle

Never build features before the foundation that supports them.

Cortex is built from the inside out.

Core before Interface.

Platform before Product.

Automation before Convenience.

---

# Phase 0

## Foundation

Objective

Build a stable operating platform.

Milestones

✅ Repository Structure

✅ Runtime — validated by two unattended scheduled fires (2026-07-25,
2026-07-26), one completing a full 17-stage cycle end-to-end with a real
YouTube publish

✅ Queue — atomic claim, checkpoint-resume, bounded auto-retry all
shipped and validated

✅ Scheduler — validated firing autonomously on its own schedule, twice,
with zero manual enqueue

✅ Orchestrator — same evidence as Runtime; `pipeline.py`'s frozen
10-agent order has now run unattended end-to-end

🟡 Configuration System

🟡 Secret Management

🟡 Provider Layer

🟡 Skills Framework

Exit Criteria

The platform can execute autonomous jobs reliably. — **Met**, per the
evidence above. Sustained-operation evidence (beyond two fires) is still
accumulating; see `CURRENT_STATE.md` Autonomous Runtime.

---

# Phase 1

## Autonomous Production

Objective

Enable Cortex to produce complete media products autonomously.

Capabilities

- Discovery
- Research
- Verification
- Narrative Design
- Script Production
- Voice Production
- Visual Production
- Assembly
- Publishing

Exit Criteria

One complete production cycle executes without routine human
intervention. — **Met** (2026-07-25T15:14:49Z-15:37:32Z, run `99391f0e`,
episode `ep_4e35674f`: all 17 stages completed, real YouTube publish
confirmed live). Multi-platform publishing beyond YouTube is now the
open item within this phase — see `CURRENT_STATE.md` Publishing —
Multi-Platform for TikTok (Upload-to-Inbox operational, Direct Post
blocked on TikTok app review), Instagram (code ready, blocked on
`META_STAGING_BASE_URL` config), and Facebook (verification in progress,
paused for burn-in priority; scope re-authorization likely required).

---

# Phase 2

## Autonomous Runtime

Objective

Remove manual execution.

Capabilities

- Background Workers
- Scheduling
- Automatic Queue Processing
- Failure Recovery
- Resume Interrupted Jobs
- Health Monitoring

Exit Criteria

Cortex runs continuously. — Strongly evidenced (two consecutive
unattended daily fires, checkpoint-resume, bounded auto-retry, and
publish-idempotency all shipped and validated), not yet fully proven —
"continuously" implies sustained operation over a longer window than the
~24 hours observed so far. This phase's exit evidence is confirmed per
the entry-evidence gate that opened Phase 3 (see `Roadmap Governance`
below); sustained-operation burn-in continues in parallel, tracked
against the Release Candidate Criteria bar rather than as this phase's
own open item.

---

# Phase 3

## Production Hardening

Status: **Active — current phase.** See `CURRENT_PHASE.md` and
`CURRENT_PROGRESS.md` for the live backlog and status.

Objective

Transform Cortex from "working" into "safe to run unattended for weeks."
This phase is **not** feature development — it exists only to improve the
production reliability, observability, and recoverability of what Phases
0–2 already built.

This phase is gating: per Working Rules below, Phase 4 (Analytics) and
Phase 5 (Learning Engine) — and any Dashboard/UX evolution — must not
begin until this phase's P0 backlog is complete.

Entry Evidence (Operational Validation)

Per the entry-evidence rule in `Roadmap Governance` below, this phase
opened only once Phase 1–2's exit criteria were confirmed with direct
evidence, not assumption:

✅ Documentation Closure — Cortex Constitution adopted, Architecture
Alignment Report reviewed, ADR-008 recorded (2026-07-25); documentation
held closed until implementation materially changes reality.

✅ External Blockers Identified — `docs/operations/PLATFORM_READINESS.md`
records the exact, evidence-based blocker for each non-YouTube platform
(TikTok: app review; Instagram: `META_STAGING_BASE_URL` config; Facebook:
operator re-authorization). None is a code defect.

✅ Burn-in Started — `docs/operations/BURN_IN_LOG.md`: two consecutive
unattended `hypogeum_daily` fires observed (2026-07-25, 2026-07-26), one
completing a full 17-stage cycle end-to-end with a real YouTube publish.

✅ Readiness Audit Complete — `docs/operations/BURN_IN_READINESS_AUDIT.md`
(2026-07-26), a read-only audit of live DB/filesystem state establishing
what is genuinely stale versus live before burn-in continues.

Capabilities (P0 — mandatory before Release Candidate)

1. **Log Rotation** — `pipeline.log` has grown unrotated to tens of GB
   (see `CURRENT_STATE.md`, memory `project_log_isolation_debt`).
   Automatic rotation: retention policy, maximum size, archive strategy,
   one-time remediation of the existing oversized file.

2. **Recovery System** — current DB backup process is manual and scoped
   only to the database. Scheduled backup + restore verification +
   backup health reporting, expanded to cover configuration/`.env`
   backup (a genuine gap — outside the DB, outside git) and an explicit
   restore-safety guarantee: restoring a backup must never cause a
   re-publish (idempotency keys in `publishing_outcomes` must fail safe
   toward "already published," not "not yet published"). Does **not**
   duplicate runtime-state recovery (checkpoint-resume, already shipped)
   or attempt full artifact/render backup (deferred — see P1).

3. **Runtime Observability** — monitoring and alerting are one
   capability, not two: a signals layer (worker heartbeat, scheduler
   heartbeat, queue growth, stuck-run detection, repeated-failure
   detection, provider-failure detection, disk/memory/CPU usage, a
   health-check endpoint) with an alerting layer built strictly on top
   of it (operator alerts for worker offline, scheduler stopped,
   repeated failures, database issues, storage issues, provider
   outages). Extends the monitoring gap already flagged under Phase 2 in
   `CURRENT_STATE.md`.

4. **Recovery & Self-Healing** — verification and extension of existing
   recovery mechanisms only, not a rebuild. Worker/scheduler auto-restart
   is already handled by launchd supervision (validated against a real
   hard-kill); this item does not re-scope that. What's new: verifying
   and extending orphan/stuck-run reconciliation coverage (the
   2026-07-26 readiness audit found 6 genuinely orphaned
   `stage_executions` rows predating the existing H1 reconciliation
   mechanism), and wiring exhausted-retry escalation into the alerting
   layer above so a give-up event is never silent.

5. **Startup & Shutdown Verification** — automatic preflight gate at
   worker startup, wiring the checks that already exist
   (`hypogeum/setup_preflight.py`, `run_acceptance_audit.py`,
   `run_production_readiness_audit.py`) into automatic execution rather
   than manual invocation; plus graceful SIGTERM handling so a stopped
   worker checkpoints/releases its claim instead of leaving an orphaned
   row (reducing what item 4's reconciliation has to clean up after the
   fact).

P1 (after P0)

- **Operational Profiles** (renamed from "Deployment Modes") — a
  profile-driven config-resolution mechanism, populated today with only
  Development/Testing/Staging/Production, designed so Local/Cloud/SaaS/
  Multi-channel profiles can be added later (Phase 8/9 territory)
  without a further rename.
- **Model and Provider Decoupling** — architectural preparation only;
  Cortex must not become hard-dependent on today's models, providers, or
  APIs, since all three will become obsolete, change price, or disappear.
  Scope: no hard-coded production model dependency; a versioned
  model/provider registry; capability-based routing; configurable
  fallback chains; model lifecycle status (active/deprecated/
  sunset); deprecation-safe configuration; a provider/model decision
  audit trail; the ability to replace a model through configuration
  rather than agent redevelopment. Explicitly does **not** include
  autonomous model discovery or benchmarking — that is Phase 6 (Factory
  Capability Evolution), gated separately and much later. This item is
  the prerequisite substrate Phase 6 will consume; it has standalone
  value even if Phase 6 is delayed indefinitely.
- Operational Health Dashboard — the presentation layer built on top of
  P0 item 3's raw signals; not itself a P0 requirement.
- Deployment Runbook improvements.
- Automated queue/run janitor — scheduled archival of terminal-state
  `run_queue`/`runs` rows past a retention window (addresses the 47
  historical rows the readiness audit had to interpret manually).
- Artifact/asset backup — risk assessment only at this stage (`.hypogeum-assets/episodes/`
  renders are outside the DB and outside git; full backup is deferred
  pending a deliberate cost/benefit call, not silently dropped).
- Maintenance Mode — an operator-toggleable switch that stops new queue
  claims while letting in-flight runs finish (useful before deploys/
  migrations).

P2 (after P1)

Remaining operational technical debt only (duplicated enums, cleanup,
maintainability). No architectural redesign.

Working Rules

Do not begin Analytics, Channel Learning, Factory Learning, Dashboard
evolution, UX improvements, or other new features until this phase's P0
is complete.

Exit Criteria

This phase's exit criteria are the Release Candidate Criteria — see
that section near the end of this document. There is no separate bar.

---

# Phase 4

## Analytics

**Blocked until Phase 3 (Production Hardening) P0 is complete.**

Objective

Understand performance.

Capabilities

- Platform Analytics
- KPI Tracking
- Performance Reports
- Cross-platform Analytics

Exit Criteria

Every published asset generates measurable feedback.

---

# Phase 5

## Learning Engine

**Blocked until Phase 3 (Production Hardening) P0 is complete.**

Objective

Use analytics to improve future decisions.

Capabilities

- Factory Learning
- Channel Learning
- Provider Learning — passive evidence collection only (quality, price,
  latency, reliability, failure patterns, prompt/model compatibility,
  deprecation risk). The active discovery/benchmark/promotion loop that
  consumes this evidence is Phase 6 (Factory Capability Evolution), not
  part of this phase. This evidence is Factory Learning, cross-channel,
  owned by Factory OS — never placed inside Hypogeum or any Channel
  Package (Channel Learning stays scoped to channel-specific audience,
  brand, editorial, content, and performance learning only).
- Prompt Learning
- Topic Learning
- Thumbnail Learning

Exit Criteria

Future executions improve based on historical evidence.

---

# Phase 6

## Factory Capability Evolution

Objective

Ensure Cortex never becomes hard-dependent on today's models, providers,
APIs, or skills. Models will become obsolete, prices will change,
providers will disappear, and better capabilities will emerge — this
phase continuously discovers, evaluates, tests, recommends, promotes,
and replaces them.

Owned by Factory OS, not any Channel Package — this capability's
knowledge (provider quality, model quality, price, latency, reliability,
failure patterns, prompt/model compatibility, skill effectiveness,
deprecation risk) is cross-channel Factory Learning, never
Hypogeum-specific or Channel-Package-specific data. Supervised by a new
agent, the **Factory Evolution Director** (named to match the existing
Director convention — Research Director, Discovery Director, Thumbnail
Director — for cross-cutting orchestration roles; "Capability
Intelligence Agent" was considered and rejected for consistency reasons,
see `DECISION_LOG.md` ADR-012).

Gating condition: this phase does not begin until Phase 4 (Analytics)
and Phase 5 (Learning Engine, specifically Factory Learning) have
produced reliable evidence — an empty registry has nothing trustworthy
to benchmark against. Not part of current P0 implementation scope; not
to be implemented until this phase is actually reached.

Depends on the substrate built in Phase 3's "Model and Provider
Decoupling" P1 item (versioned registry, capability-based routing,
fallback chains) — without that substrate, this phase would have nothing
safe to promote into.

Capabilities

- Discover new models, providers, APIs, and reusable skills
- Detect pricing, capability, API, and availability changes
- Detect deprecated or removed models
- Maintain a versioned capability registry
- Run controlled benchmark suites
- Compare quality, cost, speed, reliability, and prompt compatibility
- Preserve production baselines
- Recommend migrations
- Run shadow tests
- Perform canary rollout
- Monitor promoted candidates
- Roll back automatically when acceptance criteria fail
- Preserve evidence and decision history
- Update Factory Learning from every experiment

Lifecycle

Discover → Evaluate → Compare → Recommend → Shadow Test → Canary Release
→ Promote → Monitor → Rollback when required

Exit Criteria

Cortex can discover, evaluate, and safely promote a replacement
model/provider/skill without manual agent redevelopment, backed by
shadow-test and canary evidence, with automatic rollback proven to work.

---

# Phase 7

## Asset Intelligence

Objective

Treat generated assets as reusable knowledge.

Capabilities

- Asset Registry
- Prompt Registry
- Archive
- Asset Search
- Duplicate Detection
- Storage Optimization

Exit Criteria

Previously generated assets are reused whenever appropriate.

---

# Phase 8

## Multi-Channel Platform

Objective

Support multiple independent media organizations.

Capabilities

- Channel Packages
- Channel Isolation
- Shared Infrastructure
- Shared Runtime
- Independent Learning

Exit Criteria

Multiple channels run simultaneously without interference.

---

# Phase 9

## Platform Productization

Objective

Transform Cortex into a commercial platform.

Capabilities

- Organizations
- User Accounts
- Roles
- Permissions
- White-label
- Licensing
- Billing
- Usage Tracking

Exit Criteria

External organizations can operate Cortex independently.

---

# Phase 10

## Ecosystem

Objective

Expand Cortex beyond the core platform.

Capabilities

- SDK
- Public API
- Plugin System
- Marketplace
- Community Skills
- Third-party Integrations

Exit Criteria

External developers can extend Cortex safely.

---

# Continuous Objectives

The following objectives never end.

- Improve quality.
- Reduce cost.
- Reduce latency.
- Reduce human intervention.
- Improve reliability.
- Increase autonomy.

These objectives apply to every phase.

---

# Interfaces (Cross-Cutting, Not a Phase)

Interfaces — Dashboard, CLI, REST API, SDK, Webhooks (`CORTEX_ARCHITECTURE.md`
Layer 1) — are not tied to a single phase above. They expose whatever
capability the current phase has actually delivered, and evolve
continuously alongside it, rather than shipping on their own schedule.

The Dashboard specifically already exists today as the primary
operator-facing surface (`CLAUDE.md`: "the primary way to operate the
factory") and grows in scope as each phase above lands — it is present
from Phase 0 onward, not deferred to any later phase. Phase 9 (Platform
Productization) is the one place Dashboard work *does* get new,
phase-specific scope: multi-tenant, white-label dashboard surfaces for
external organizations, per that phase's own capability list.

Every Interface must obey `CORTEX_ARCHITECTURE.md`'s Dependency Direction
rules unconditionally: no business logic, channel-agnostic by
construction (the Core "knows nothing about any specific channel" —
Interfaces inherit that constraint), replaceable without changing Core.
Any Dashboard work that currently violates this (see `CURRENT_STATE.md`
for the specific known instance) is architectural debt to close, not a
precedent to extend.

---

# Release Candidate Criteria

Do not evaluate Cortex for Release Candidate status until **all** of the
following are true:

- ☑ Production Hardening (Phase 3) P0 complete (log rotation, recovery
  system, runtime observability, recovery & self-healing, startup &
  shutdown verification) — shipped 2026-07-27, see `CURRENT_PROGRESS.md`.
  Live burn-in observation of this new code is still needed before the
  "no production defects during burn-in" item below can be checked.
- ☐ External publishing blockers closed OR formally accepted
  (TikTok/Instagram/Facebook — see `docs/operations/PLATFORM_READINESS.md`)
- ☐ 5–10 successful unattended scheduled runs (2 observed so far — see
  `docs/operations/BURN_IN_LOG.md`)
- ☐ No production defects during burn-in
- ☐ Stable runtime demonstrated over the full observation window
- ☐ Runbooks complete
- ☐ Monitoring operational
- ☐ Automated backups operational
- ☐ Log rotation operational

This list supersedes any prior informal RC discussion. See
`CURRENT_PROGRESS.md` for live status against each item.

---

# Roadmap Governance

A phase is considered complete only when its exit criteria are satisfied.

New features must not bypass unfinished foundational work unless they directly unblock the current phase.

**Entry-evidence rule**: before a phase transition, the closing phase's
exit criteria must be confirmed with direct evidence (an audit, a
burn-in record, or equivalent), not asserted. This evidence is recorded
as an "Entry Evidence" subsection of the phase being entered — it is not
given its own phase number. (Phase 3's Entry Evidence section above is
the first application of this rule; apply it at every future transition,
including into Phase 4 and beyond.)

---

# Current Focus

Primary Objective (superseded 2026-07-26 by the Operational Validation
audit and the subsequent Production Hardening architecture review — see
below)

Achieve reliable autonomous end-to-end production with minimal human intervention.

This objective is achieved for a single cycle (2026-07-25, real YouTube
publish, unattended) and reinforced by a second unattended fire
(2026-07-26). The Operational Validation audit (2026-07-26) confirmed
implementation work is complete but identified that Cortex is not yet
safe to run unattended for weeks without further hardening. The primary
objective has shifted to:

1. Complete Production Hardening (Phase 3) P0: log rotation, recovery
   system, runtime observability, recovery & self-healing, startup &
   shutdown verification — implemented one capability at a time, in that
   order. This is now the single top priority — see `CURRENT_PHASE.md`.
2. Continue burn-in observation toward the 5–10 unattended-run bar in
   Release Candidate Criteria above.
3. Close or formally accept the remaining external publishing blockers
   (TikTok/Instagram/Facebook).

No Analytics, Learning, Dashboard evolution, or other new-feature work
until item 1 is complete (see Phase 3 Working Rules above). Everything
else is secondary until these are achieved.

---

# Authority

This document defines the official implementation order of Cortex.

If implementation priorities conflict with this roadmap,

the roadmap takes precedence unless superseded by an approved architectural decision.
