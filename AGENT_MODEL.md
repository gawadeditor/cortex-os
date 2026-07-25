# AGENT_MODEL.md

# Cortex Agent Model

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the Agent Model used by Cortex.

Agents are runtime executors.

They perform work on behalf of Services during an execution.

Agents are not architectural layers.

Agents are not business domains.

Agents exist only while work is being performed.

---

# Core Principle

Services own business logic.

Skills provide expertise.

Agents execute work.

The Cortex Core coordinates everything.

---

# Definition

An Agent is a temporary execution unit created by the Runtime to perform one or more tasks.

Agents are disposable.

They are created when needed.

They are destroyed after completion.

---

# Responsibilities

An Agent is responsible for:

- Receiving work.
- Executing assigned tasks.
- Applying relevant Skills.
- Producing outputs.
- Reporting results.

Agents do not make architectural decisions.

Agents do not own workflows.

Agents do not own business domains.

---

# Agent Lifecycle

Every Agent follows the same lifecycle.

```
Created

↓

Initialized

↓

Receive Task

↓

Load Required Skills

↓

Execute

↓

Validate Output

↓

Return Result

↓

Terminate
```

Agents never survive beyond their execution.

---

# Agent Scope

An Agent operates inside exactly one Service.

Examples

Research Service

↓

Research Agent

---

Publishing Service

↓

Publishing Agent

---

Analytics Service

↓

Analytics Agent

An Agent never belongs to multiple Services simultaneously.

---

# Agent Types

Cortex does not require predefined Agent types.

Each Service may create Agents appropriate for its work.

Examples

Research Agent

Script Agent

Publishing Agent

Thumbnail Agent

Analytics Agent

Archive Agent

These are execution roles rather than architectural components.

---

# Task Execution

Agents execute Tasks.

```
Task

↓

Input Validation

↓

Skill Application

↓

Execution

↓

Output Validation

↓

Result
```

Tasks are the smallest executable unit.

---

# Skills

Agents may load one or more Skills.

Example

Research Agent

↓

Historical Research Skill

↓

Fact Verification Skill

↓

Citation Skill

↓

Result

Agents never contain permanent expertise.

Expertise belongs to Skills.

---

# Context

Agents receive a limited Execution Context.

Examples

- Task Context
- Objectives
- Policies
- Inputs
- Relevant Learning
- Provider Configuration

Agents receive only the information required for their task.

---

# Communication

Agents never communicate directly.

Allowed

Core

↓

Service

↓

Agent

↓

Core

Forbidden

Agent

↓

Agent

Agent-to-Agent communication is prohibited.

---

# State

Agents are stateless.

Persistent information belongs to:

- Cortex Storage
- Factory Learning
- Channel Learning

Temporary information belongs to the Execution Context.

---

# Parallel Execution

Multiple Agents may execute simultaneously.

Each Agent owns:

- Independent execution
- Independent temporary memory
- Independent resources
- Independent outputs

Agents must never share temporary state.

---

# Error Handling

Agents detect execution failures.

They report failures to the Service.

The Service reports execution status to the Core.

Recovery decisions belong to the Core.

---

# Observability

Every Agent reports:

- Start time
- End time
- Duration
- Status
- Provider usage
- Resource usage
- Errors

This information supports monitoring and optimization.

---

# Replaceability

An Agent implementation may change completely without changing:

- Cortex Core
- Services
- Skills
- Channel Packages

Only the execution behavior changes.

---

# Relationship to Other Components

Execution Flow

Core

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

The Core coordinates.

The Service owns business logic.

The Agent executes.

The Skill provides expertise.

---

# Design Rules

Rule 1

Agents are temporary.

Rule 2

Agents never own business logic.

Rule 3

Agents never own expertise.

Rule 4

Agents execute Tasks.

Rule 5

Agents are stateless.

Rule 6

Agents communicate only through the Core and their owning Service.

Rule 7

Agents are replaceable.

---

# Authority

This document defines the official Agent Model of Cortex.

Agents are runtime execution units.

They exist only to execute work.

All permanent knowledge, identity, and business ownership remain outside the Agent.