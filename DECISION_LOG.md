# DECISION_LOG.md

# Cortex Architectural Decision Log

Version: 1.0

Status: Living Document

Authority: High

---

# Purpose

This document records significant architectural and product decisions made during the evolution of Cortex.

Its purpose is to preserve architectural intent.

Every major decision should include:

- The decision itself.
- Why it was made.
- The alternatives considered.
- The expected long-term impact.

This document prevents the project from repeatedly solving the same problems.

---

# Decision Format

Every decision follows the same structure.

---

## Decision ID

Unique identifier.

Example:

ADR-001

---

## Date

Date the decision became official.

---

## Status

Accepted

Superseded

Deprecated

Rejected

---

## Context

What problem existed?

---

## Decision

What was decided?

---

## Alternatives Considered

Which alternatives were evaluated?

Why were they rejected?

---

## Consequences

Positive outcomes.

Trade-offs.

Limitations.

---

# Decisions

---

# ADR-001

## Title

Cortex is a platform, not a channel.

Status

Accepted

Context

The original implementation evolved around a single documentary channel.

This risked coupling the platform to one product.

Decision

Cortex is defined as a platform capable of operating any media organization.

Channels are external packages.

Consequences

Positive

- Unlimited scalability.
- Reusable platform.
- Separation of concerns.

Trade-offs

- Increased architectural complexity.

---

# ADR-002

## Title

Channel identity lives outside Cortex.

Status

Accepted

Context

Editorial identity, prompts and branding were initially mixed with platform logic.

Decision

Every channel is represented by an independent Channel Package.

Consequences

Positive

- Clean separation.
- Easier maintenance.
- Independent channels.

---

# ADR-003

## Title

Factory Learning and Channel Learning are separate systems.

Status

Accepted

Context

Different kinds of knowledge evolve independently.

Decision

Operational knowledge belongs to Cortex.

Editorial knowledge belongs to Channel Packages.

Consequences

Positive

Better learning isolation.

Reusable operational intelligence.

---

# ADR-004

## Title

Interfaces never own business logic.

Status

Accepted

Context

Dashboard growth created pressure to place logic inside UI.

Decision

All business logic remains inside Core or Services.

Interfaces only visualize and issue commands.

Consequences

Positive

Replaceable interfaces.

Cleaner architecture.

---

# ADR-005

## Title

Skills are the primary extension mechanism.

Status

Accepted

Context

Reusable expertise should not require modifying the Core.

Decision

Skills encapsulate reusable knowledge and behavior.

Core coordinates execution.

Consequences

Positive

Simpler maintenance.

Independent evolution.

Reusable expertise.

---

# ADR-006

## Title

Channel Packages are loaded at runtime.

Status

Accepted

Context

Permanent coupling between Core and channels limits scalability.

Decision

Channel Packages are loaded into an execution context at runtime.

Nothing from a package permanently modifies Cortex.

Consequences

Positive

Supports unlimited channels.

Independent deployment.

---

# ADR-007

## Title

Architecture precedes implementation.

Status

Accepted

Context

Implementation work repeatedly exposed missing architectural decisions.

Decision

No major implementation begins without documenting the architectural decision first.

Consequences

Positive

Less rework.

Better consistency.

Improved long-term maintainability.

---

# ADR-008

## Title

Service Layer convergence is incremental and roadmap-subordinate.

Status

Accepted

Context

The Core → Service → Agent boundary defined by `SERVICE_MODEL.md` /
`AGENT_MODEL.md` / `EXECUTION_MODEL.md` does not exist in the current
implementation. `pipeline.py::run_pipeline()` calls each agent module
directly, with no intermediate Service abstraction (Architecture Alignment
Report, Major Architectural Conflict #1). A disruptive rewrite to close
this gap immediately was considered and rejected as inconsistent with
Progressive Evolution.

Decision

The Service layer is built incrementally, never as a standalone rewrite:

- New code is built Service-shaped from the start.
- Existing code converges to a Service boundary only when touched for a
  real reason (a bug fix or feature), never touched solely to relocate it.
- Architecture defines HOW code is built when it is built. The Roadmap
  defines WHAT is built next and WHEN. Neither constraint overrides the
  other. A capability is never prioritized ahead of its roadmap position
  because building it as a Service would be easier or lower-risk than
  converging existing code.

Full mapping of target Services to current modules, and the specific
opportunistic-convergence candidates, are recorded in
`docs/architecture/SERVICE_LAYER_MIGRATION_PLAN.md`.

Alternatives Considered

- Immediate full Service-layer extraction across all ten pipeline agents —
  rejected: high regression risk to a frozen, production-validated stage
  order, with no roadmap item currently requiring it.
- Deferring the Service layer entirely to Phase 6 (Multi-Channel
  Platform) — rejected: leaves new Phase 3/4 work (Analytics, Learning)
  built the wrong shape, which is more expensive to correct later than to
  build correctly the first time.

Consequences

Positive

- Zero regression risk to existing production code.
- Analytics (Phase 3) and Learning (Phase 4) become the first real,
  working examples of the Service Model, at no added cost over building
  them anyway.
- Existing agent modules converge over time without a dedicated
  migration effort.

Trade-offs

- The Service layer will exist unevenly for a period — some domains
  Service-shaped, most still direct agent-module calls — until enough
  opportunistic touches accumulate.
- Requires discipline at each future touch point to actually perform the
  convergence rather than deferring it again.

---

# ADR-009

## Title

Dashboard UI/UX work requires the UI/UX Pro Max Skill.

Status

Accepted

Context

The Dashboard is Cortex's primary operator-facing Interface
(`CORTEX_ARCHITECTURE.md` Layer 1) and has substantial pre-existing
design work (`docs/dashboard-architecture-system.md` and related
documents) predating the current Constitution. No consistent design
discipline was previously mandated for Dashboard UI/UX work specifically.

Decision

Any model or agent assigned Dashboard UX/UI design or implementation must
first install, load, and follow the UI/UX Pro Max Skill before starting.
This is treated as a mandatory implementation dependency, not an optional
reference — per `SKILLS_SPEC.md`'s ownership of Skill requirements
(`GOVERNANCE.md`: "Skills → Skills Specification").

This is recorded here, not in `CURRENT_STATE.md` — `CURRENT_STATE.md`
describes what exists today; this is forward-looking policy governing
future Dashboard implementation work, which belongs in the Decision Log
and Skills governance instead.

Known open dependency: the UI/UX Pro Max Skill does not yet exist in this
repository or at the user's `~/.claude/skills/` level (verified
2026-07-26). This decision is accepted and in effect; the skill itself
must still be installed/created before any Dashboard UI/UX work can
actually comply with it.

Consequences

Positive

- Consistent design discipline across all future Dashboard work,
  regardless of which model/agent performs it.
- Design quality is no longer implicitly optional or reviewer-dependent.

Trade-offs

- Blocks Dashboard UI/UX work until the skill is actually installed.
- Adds one more mandatory-skill dependency to track (see `SKILLS_SPEC.md`).

---

# ADR-010

## Title

Adopt the Interface Contract Model as canonical, governing all present
and future Interfaces.

Status

Accepted

Context

`CORTEX_ARCHITECTURE.md` establishes that Interfaces (Dashboard, CLI,
REST API, SDK, Webhooks) never own business logic and depend only on the
Core, but no document specified the concrete shape of that dependency —
what a request looks like, how long-running work is tracked, who owns
authentication versus authorization, or how errors and retries are
represented consistently across Interfaces. This gap was directly
implicated in the structural finding already recorded against
Conflict #4 in `docs/architecture/CORTEX_ALIGNMENT_REPORT.md`: without an
explicit contract, an Interface's own backend can drift into owning
business rules (eligibility, RBAC, approval) simply because no
alternative was specified.

Reviewed for conflicts against `PROJECT_NORTH_STAR.md`,
`FACTORY_PHILOSOPHY.md`, `CORTEX_ARCHITECTURE.md`, `DESIGN_PRINCIPLES.md`,
`EXECUTION_MODEL.md`, `SERVICE_MODEL.md`, `WORKFLOW_MODEL.md`,
`SKILLS_SPEC.md`, and `GOVERNANCE.md`. None were found; the new document
elaborates rules those documents already imply (Interfaces reach only the
Core; the Core owns policy enforcement) rather than overriding any of
them.

Decision

`INTERFACE_CONTRACT_MODEL.md` is adopted as canonical (Documentation
Hierarchy position 14, between `WORKFLOW_MODEL` and `DATA_MODEL`). It
defines: Interface responsibilities and forbidden behavior; the
Interface → Core and Core → Service interaction models; Command, Query,
and Event contract shapes; the split between Interface-owned shape
validation and Core-owned business/policy validation; the ownership
split between Interface+Infrastructure-owned Authentication and
Core-owned Authorization; long-running-operation and progress-reporting
conventions reusing `EXECUTION_MODEL.md`'s existing state vocabulary;
shared error-handling categories; idempotency requirements for Commands;
and worked examples showing the Dashboard, CLI, and REST API all
producing identical outcomes for identical requests.

This is architecture only. No implementation, runtime, or Dashboard code
changed as part of this decision. It governs how future Dashboard, REST
API, CLI, and SDK work must be built; it does not itself correct any
currently-running code, including the Conflict #4 Dashboard
business-logic finding, which remains open and separately tracked.

Alternatives Considered

- Leaving the contract implicit in `CORTEX_ARCHITECTURE.md`'s existing
  layer diagram — rejected: the diagram states the dependency direction
  but not the request/response shape, which is precisely what allowed
  the Conflict #4 drift to occur undetected.
- Folding this content into `CORTEX_ARCHITECTURE.md` directly rather than
  a new document — rejected: `CORTEX_ARCHITECTURE.md` defines permanent
  structural boundaries at the layer level; the Interface contract is a
  more detailed, Interface-specific specification, matching the existing
  pattern of `SERVICE_MODEL.md` and `WORKFLOW_MODEL.md` each detailing
  one layer/concept beyond what `CORTEX_ARCHITECTURE.md` itself states.

Consequences

Positive

- Every future Interface has one specification to build against, instead
  of inferring contract shape independently per Interface.
- Provides the explicit rule (Authorization is Core-owned, never
  Interface-owned) needed to resolve Conflict #4 correctly whenever that
  work resumes.
- Multi-interface consistency (Dashboard, CLI, REST API, SDK all
  producing identical outcomes) becomes a structural guarantee rather
  than a convention to remember.

Trade-offs

- Existing Interface code (the Dashboard, `run_pipeline.py`) was not
  audited or changed against this contract as part of this decision;
  conformance is a future, separately-scoped effort.
- Introduces new vocabulary (Command, Query, Event, Execution Reference)
  that existing informal usage in the codebase does not yet use
  consistently.

---

# ADR-011

## Title

TikTok publishing is capability-gated: Direct Post vs. Upload-to-Inbox,
selected from the credential's actual granted scope.

Status

Accepted

Context

TikTok's Content Posting API has two distinct flows gated by app-review
status: Direct Post (`video.publish` scope, fully autonomous) and
Upload-to-Inbox (`video.upload` scope only, delivers a draft to the
creator's TikTok inbox — a human must open the app and tap "Post" to
finish). The current app has never passed TikTok's review for
`video.publish`; only `video.upload` is granted (confirmed via the
stored credential's `scope` field and a real live HTTP 400 test). The
existing code implemented Direct Post only, and — independently of the
scope question — had two real defects (a wrong API path missing
`/publish/`, and an incorrect chunk-size calculation for videos under
10MB) that were masking this from ever being cleanly observed.

Decision

`hypogeum/publishing/tiktok.py` gains `resolve_tiktok_publish_mode()`,
which reads the credential's granted scope and returns which flow is
authorized. Direct Post is never attempted when `video.publish` is
absent. When only `video.upload` is granted, the pipeline uses
Upload-to-Inbox and records the result as a new terminal status,
`uploaded_to_inbox` (added to `publishing_outcomes.publishing_status`,
the pipeline-level `publishing_record` schema, and every in-code enum
copy — four call sites in total, found by grep, not assumed), distinct
from `published`/`failed`/`skipped`. `PublishingRecord`'s
production_ready computation already only treats the literal string
`"published"` as success, so `uploaded_to_inbox` is structurally
prevented from ever being mistaken for full autonomous publishing.

Alternatives Considered

- Leaving TikTok as Direct-Post-only and treating it as simply "blocked
  external, like Facebook" — rejected: unlike Facebook's scope gap
  (which blocks the endpoint entirely), Upload-to-Inbox is a real,
  currently-authorized, working flow that delivers genuine (if
  semi-autonomous) production value. Refusing to use it would discard
  a capability the app already has.
- Reusing the existing `skipped` status for inbox uploads — rejected:
  `skipped` already means "this deliverable was not attempted"
  (platform disabled, asset missing); an inbox upload is a real,
  successful API operation that produced a durable server-side result
  (a `publish_id`) — conflating the two would make idempotency and
  auditing ambiguous.

Consequences

Positive

- TikTok produces real, verified production value today
  (Upload-to-Inbox) without any code change required once TikTok grants
  `video.publish` — `resolve_tiktok_publish_mode()` will simply switch
  flows on its own the next time it's called.
- The distinction between "fully autonomous" and "semi-autonomous,
  awaiting a human step" is now structural (a distinct status value),
  not something an operator has to infer from context.

Trade-offs

- TikTok cannot be considered part of "fully autonomous, no manual
  intervention" multi-platform publishing until TikTok's own app review
  grants `video.publish` — an external dependency, tracked in
  `CURRENT_STATE.md` Publishing — Multi-Platform, not resolvable by
  further code work.

---

# ADR-012

## Title

Model/provider decoupling is split into two roadmap phases: an
architectural-substrate P1 item now (Production Hardening), and a
separate, later, evidence-gated phase (Factory Capability Evolution) for
the autonomous discovery/promotion loop.

Status

Accepted

Context

The Production Hardening architecture review (2026-07-26) that finalized
`ROADMAP.md` Phase 3 surfaced a distinct risk not covered by that
phase's reliability-only scope: Cortex must not become permanently
dependent on today's models, providers, or APIs. Models will become
obsolete, prices will change, providers will disappear, and better
capabilities will emerge over the platform's lifetime — this is a
multi-year concern, not a burn-in concern. Today, replacing a model
requires touching config defaults and, in places, agent code directly,
rather than a single configuration decision.
`MASTER_CAPABILITY_BLUEPRINT.md`'s existing LEVEL 9 (Provider Platform)
already listed "Model Registry" and "Provider Benchmarking" as 🔴
Planned, but neither had a roadmap phase or an owning agent.

Decision

Split the concern into two phases, deliberately kept apart:

1. **`ROADMAP.md` Phase 3 (Production Hardening) gains a new P1 item,
   "Model and Provider Decoupling"** — architectural preparation only: no
   hard-coded production model dependency, a versioned model/provider
   registry, capability-based routing, configurable fallback chains,
   model lifecycle status, deprecation-safe configuration, a
   provider/model decision audit trail, and the ability to replace a
   model through configuration rather than agent redevelopment. This
   item does **not** include autonomous discovery or benchmarking.

2. **A new `ROADMAP.md` Phase 6, "Factory Capability Evolution"** —
   owned by Factory OS, never by any Channel Package, since its
   knowledge (provider/model quality, price, latency, reliability,
   failure patterns, prompt/model compatibility, skill effectiveness,
   deprecation risk) is cross-channel Factory Learning. Supervised by a
   new agent, the **Factory Evolution Director**. Lifecycle: Discover →
   Evaluate → Compare → Recommend → Shadow Test → Canary Release →
   Promote → Monitor → Rollback when required. Gated: does not begin
   until Phase 4 (Analytics) and Phase 5 (Learning Engine, specifically
   Factory Learning) have produced reliable evidence — an empty registry
   has nothing trustworthy to benchmark against. Depends on item 1's
   substrate; not to be implemented before it, and not to be implemented
   at all until the phase is actually reached.

`MASTER_CAPABILITY_BLUEPRINT.md` LEVEL 9 gained explicit rows for the
capabilities this decision introduces (capability-based routing,
fallback chains, lifecycle status, deprecation detection, shadow
testing, canary rollout, automatic rollback, decision audit trail), and
an ownership note was added to LEVEL 4 (Learning) clarifying Factory
Learning vs. Channel Learning scope.

Alternatives Considered

- Building Factory Capability Evolution's full autonomous loop inside
  current P0 — rejected: Production Hardening P0 is explicitly not
  feature development, and Analytics/Factory Learning have not shipped
  yet, so there is no real evidence to evaluate promotion decisions
  against. Building the loop now means it operates on no real data.
- Naming the supervising agent "Capability Intelligence Agent" — 
  considered, not chosen: existing pipeline stages use a "Director"
  naming convention for cross-cutting orchestration roles (Research
  Director, Discovery Director, Thumbnail Director); "Factory Evolution
  Director" is consistent with that pattern.
- Folding Factory Capability Evolution into Phase 5 (Learning Engine)
  instead of a separate phase — rejected: Phase 5's Provider Learning is
  passive evidence collection; this capability is an active discovery/
  benchmark/promotion system with its own lifecycle and rollback
  authority. Conflating the two would give one phase two incompatible
  kinds of exit criteria.

Consequences

Positive

- Model/provider dependency becomes explicit and swappable through
  configuration well before any autonomous system exists to exploit
  that swappability — the substrate has standalone value even if Phase
  6 is delayed indefinitely or never built.
- Phase 6 has a clean, evidence-gated entry condition (Phase 4 + Phase 5
  must ship first) rather than an arbitrary one.
- Learning ownership is unambiguous going forward: Factory Learning
  (cross-channel, Factory OS) vs. Channel Learning (channel-specific) —
  no future ambiguity about where provider/model evidence lives.

Trade-offs

- Two roadmap phases (Phase 3's P1 item and Phase 6) both touch
  "model/provider" concerns; a future reader must track both to get the
  full picture. Mitigated by explicit cross-references between them in
  `ROADMAP.md`.
- The registry/routing substrate may exist for a long time before Phase
  6 is reached, with no autonomous consumer during that window — pure
  architectural insurance, not immediate payoff.

---

# Decision Lifecycle

Architectural decisions are expected to evolve.

A decision may become:

Accepted

↓

Superseded

↓

Deprecated

↓

Archived

Previous decisions are never deleted.

History is preserved.

---

# Governance

Only architectural decisions belong in this document.

Implementation details belong elsewhere.

Operational issues belong elsewhere.

Bug fixes do not belong here.

---

# Authority

If an implementation conflicts with an accepted architectural decision,

either:

- the implementation must change,

or

- a new architectural decision must supersede the previous one.

Architectural history is never rewritten.

Only extended.