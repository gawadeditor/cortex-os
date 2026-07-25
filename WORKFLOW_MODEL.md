# WORKFLOW_MODEL.md

# Cortex Workflow Model

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the Workflow Model of Cortex.

A Workflow describes how Services collaborate to achieve a business objective.

A Workflow is not a Service.

A Workflow is not an Agent.

A Workflow is an execution plan.

---

# Core Principle

Services define capabilities.

Workflows define orchestration.

Agents execute tasks.

The Core coordinates everything.

---

# Definition

A Workflow is an ordered collection of business steps executed to accomplish a specific objective.

Examples include:

- Produce an Episode
- Publish Content
- Analyze Performance
- Synchronize Analytics
- Learn from Results
- Archive Assets

A Workflow coordinates Services.

It never replaces them.

---

# Responsibilities

A Workflow is responsible for:

- Defining execution order
- Defining dependencies
- Passing validated outputs
- Handling branching
- Handling retries
- Determining completion

Business logic remains inside Services.

---

# Workflow Lifecycle

Every Workflow follows the same lifecycle.

```
Created

↓

Validated

↓

Scheduled

↓

Executed

↓

Completed

↓

Archived
```

---

# Workflow Composition

A Workflow consists of one or more Stages.

```
Workflow

↓

Stage

↓

Service

↓

Agent

↓

Task
```

Each level has a single responsibility.

---

# Stage

A Stage represents one logical phase.

Examples

Discovery

Research

Script

Visual Production

Publishing

Analytics

Learning

Each Stage invokes one or more Services.

---

# Dependencies

A Stage may depend on:

- Previous Stage outputs
- Policies
- Objectives
- Execution Context

A Stage must never access future stages.

---

# Data Flow

Information always flows forward.

```
Stage A

↓

Validated Output

↓

Stage B

↓

Validated Output

↓

Stage C
```

Backward dependencies are prohibited.

---

# Branching

A Workflow may branch.

Example

```
Research

↓

Verification

↓

───────────────

↓

Script

↓

SEO

↓

Thumbnail

↓

───────────────

↓

Assembly
```

Branches execute independently until synchronization.

---

# Parallel Execution

Independent Stages may execute simultaneously.

Example

- Thumbnail
- SEO
- Metadata

These can run in parallel after Script completion.

Parallel execution is optional.

---

# Synchronization

A synchronization point waits until all required branches complete.

Example

```
Voice

↓

Visual

↓

Thumbnail

↓

Metadata

↓

──────────

↓

Publishing
```

Publishing begins only after all required outputs are available.

---

# Validation

Each Stage validates:

- Inputs
- Outputs
- Policies
- Required artifacts

Invalid outputs stop downstream execution unless recovery policies allow continuation.

---

# Failure Handling

Failures are classified.

Possible actions include:

- Retry Stage
- Skip Stage
- Substitute Service
- Abort Workflow

Recovery policies are defined by the Core.

---

# Workflow Types

Examples include:

Production Workflow

Publishing Workflow

Analytics Workflow

Learning Workflow

Maintenance Workflow

Synchronization Workflow

Additional Workflow types may be introduced without modifying the execution model.

---

# Workflow Registry

Every Workflow must be registered.

Registration includes:

- Name
- Version
- Stages
- Dependencies
- Required Services
- Entry Conditions
- Exit Conditions

---

# Reusability

A Workflow should be reusable.

The same Production Workflow should operate for different Channel Packages without modification.

Only configuration changes.

---

# Observability

Every Workflow exposes:

- Status
- Progress
- Active Stage
- Completed Stages
- Failed Stages
- Duration
- Resource Consumption

This information enables monitoring and optimization.

---

# Relationship to Other Components

Execution

↓

Workflow

↓

Stage

↓

Service

↓

Agent

↓

Task

↓

Skill

↓

Result

Each layer has a distinct responsibility.

---

# Design Rules

Rule 1

A Workflow owns orchestration.

Rule 2

A Workflow never owns business logic.

Rule 3

A Workflow coordinates Services.

Rule 4

Stages communicate only through validated outputs.

Rule 5

Workflows are versioned.

Rule 6

Workflows are reusable.

Rule 7

Workflow definitions are declarative whenever possible.

---

# Authority

This document defines the official Workflow Model of Cortex.

Every execution performed by Cortex must occur through a registered Workflow.

No execution path may bypass the Workflow Model.