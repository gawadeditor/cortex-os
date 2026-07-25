# SERVICE_MODEL.md

# Cortex Service Model

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the Service Model of Cortex.

A Service is the primary business unit of the platform.

Services implement business capabilities.

They do not define identity.

They do not own infrastructure.

They execute work within the Cortex Runtime.

---

# Definition

A Service is an independent business domain responsible for performing a specific category of work.

Examples include:

- Discovery
- Research
- Production
- Publishing
- Analytics
- Learning

Services are long-lived architectural components.

They are not temporary runtime objects.

---

# Responsibilities

Every Service is responsible for:

- Implementing one business domain.
- Exposing well-defined capabilities.
- Executing work requested by the Core.
- Producing validated outputs.
- Respecting platform policies.
- Reporting execution status.

A Service never owns platform identity.

---

# Design Principles

## Single Responsibility

Each Service owns one business domain.

A Service should have one primary reason to change.

---

## Independence

Services must be independently replaceable.

Replacing one Service should not require changing unrelated Services.

---

## Stateless Execution

Services do not retain execution state between runs.

Persistent state belongs to platform storage.

Temporary state belongs to the Execution Context.

---

## Reusability

A Service must operate correctly regardless of which Channel Package is loaded.

Business logic is reusable.

Identity is not.

---

## Deterministic Interfaces

Every Service exposes a stable contract.

Inputs and outputs must remain predictable.

Implementation may evolve.

Contracts should remain stable whenever possible.

---

# Service Lifecycle

A Service follows the same lifecycle during execution.

```
Load

↓

Initialize

↓

Receive Context

↓

Execute Tasks

↓

Validate Results

↓

Return Outputs

↓

Shutdown
```

A Service exists only for the duration required by the Runtime.

---

# Communication

Services never communicate arbitrarily.

All communication is coordinated by the Cortex Core.

Allowed

Core

↓

Service A

↓

Core

↓

Service B

Forbidden

Service A

↓

Service B

Direct communication between Services is prohibited unless explicitly defined by platform contracts.

---

# Inputs

A Service receives:

- Execution Context
- Policies
- Objectives
- Configuration
- Requests
- Previous validated outputs

A Service should never request information directly from another Service.

---

# Outputs

Every Service produces structured outputs.

Outputs should be:

- Valid
- Complete
- Traceable
- Versioned when appropriate

Outputs become inputs for downstream Services only after validation.

---

# Error Handling

A Service is responsible for identifying:

- Validation errors
- Business rule violations
- Provider failures
- Recoverable failures
- Non-recoverable failures

Recovery decisions belong to the Core.

---

# Service Categories

## Production Services

Responsible for creating media.

Examples

- Discovery
- Research
- Script
- Voice
- Visual
- Thumbnail
- Assembly

---

## Operational Services

Responsible for operating Cortex.

Examples

- Publishing
- Scheduling
- Monitoring
- Notifications
- Archive

---

## Intelligence Services

Responsible for improving decisions.

Examples

- Analytics
- Learning
- Optimization
- Recommendations

---

## Platform Services

Responsible for platform support.

Examples

- Provider Management
- Secrets
- Configuration
- Asset Registry
- Audit

---

# Composition

A Service may internally organize its work using:

- Skills
- Workers
- Pipelines
- Tasks

The internal structure is private.

Only the external contract is public.

---

# Extensibility

New Services may be introduced without changing existing Services.

Every new Service must:

- Own a distinct business domain.
- Publish a contract.
- Register with the Core.
- Respect platform policies.

---

# Observability

Every Service must expose execution information.

Examples

- Status
- Duration
- Errors
- Resource usage
- Provider usage
- Output quality metrics

Observability enables monitoring without coupling.

---

# Scalability

Multiple instances of the same Service may execute simultaneously.

A Service implementation must therefore be:

- Stateless
- Concurrent-safe
- Runtime-isolated

---

# Relationship to Other Components

Core

Coordinates Services.

Infrastructure

Supports Services.

Channel Package

Configures Services.

Skills

Enhance Services.

Agents

Execute work inside Services.

Services remain the primary architectural unit.

---

# Authority

This document defines the official Service Model of Cortex.

Every business capability within Cortex must belong to a Service.

No business logic may exist outside the Core or a Service.