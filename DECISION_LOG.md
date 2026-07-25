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