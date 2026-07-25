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