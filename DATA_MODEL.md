# DATA_MODEL.md

# Cortex Data Model

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the canonical data entities used throughout Cortex.

It establishes a common language for the platform.

Every component must refer to these entities consistently.

This document defines concepts, not database schemas.

It does not specify tables, collections, fields, or implementation details.

---

# Core Principle

Everything managed by Cortex is represented as a well-defined Entity.

Entities represent persistent concepts.

Execution creates instances of those concepts.

---

# Entity Categories

The Cortex Data Model consists of six categories.

- Platform
- Execution
- Knowledge
- Content
- Infrastructure
- Operations

---

# Platform Entities

These entities define the platform itself.

## Channel

Represents a media organization.

Examples

- Hypogeum
- Technology Daily
- Science Explained

A Channel is defined by a Channel Package.

---

## Channel Package

Represents the complete configuration of a Channel.

Contains

- Identity
- Objectives
- Editorial DNA
- Policies
- Learning
- Platform configuration

A Channel Package belongs to exactly one Channel.

---

## Service

Represents a business capability.

Examples

- Discovery
- Research
- Publishing
- Analytics

Services are permanent architectural entities.

---

## Workflow

Represents an execution plan.

A Workflow coordinates Services.

Examples

- Episode Production
- Publishing
- Analytics Synchronization

---

## Policy

Represents a rule that governs execution.

Examples

- Budget limits
- Retry policies
- Publishing restrictions
- Safety rules

---

# Execution Entities

These entities exist only while work is being performed.

---

## Execution

Represents one complete runtime execution.

Every execution has:

- Identifier
- Status
- Workflow
- Channel
- Context
- Results

Execution is temporary.

Its approved outputs become persistent.

---

## Execution Context

Represents the temporary runtime environment.

Contains

- Configuration
- Objectives
- Secrets
- Learning
- Runtime metadata

Destroyed after execution.

---

## Stage

Represents one logical phase inside a Workflow.

Examples

- Research
- Script
- Publishing

---

## Task

Represents the smallest schedulable unit of work.

Tasks are executed by Agents.

---

## Agent

Represents a temporary runtime executor.

Agents are created by the Runtime.

Agents do not persist after execution.

---

# Knowledge Entities

These entities represent reusable knowledge.

---

## Skill

Represents reusable expertise.

Examples

- Documentary Writing
- Thumbnail Design
- SEO Optimization

Skills are reusable.

---

## Factory Learning

Represents operational knowledge accumulated by Cortex.

Examples

- Provider performance
- Cost history
- Runtime optimization

Shared across Channels.

---

## Channel Learning

Represents knowledge specific to one Channel.

Examples

- Audience behavior
- Successful topics
- Thumbnail performance

Never shared.

---

# Content Entities

These entities represent media assets.

---

## Topic

Represents a production opportunity.

---

## Research

Represents collected evidence.

---

## Script

Represents structured narrative content.

---

## Voice Asset

Represents generated narration.

---

## Visual Asset

Represents generated images or video.

---

## Thumbnail

Represents the selected thumbnail.

---

## Episode

Represents the final published media product.

---

## Asset

Represents any reusable media resource.

Examples

- Image
- Video
- Audio
- Prompt
- Template

---

# Infrastructure Entities

These entities support execution.

---

## Provider

Represents an external AI capability.

Examples

- Text Generation
- Video Generation
- Voice Generation

---

## Provider Model

Represents one specific model.

Examples

- GPT
- Claude
- Veo
- Seedance

---

## Secret

Represents protected credentials.

Examples

- OAuth tokens
- API Keys
- Platform credentials

---

## Storage Resource

Represents persistent storage managed by Cortex.

Examples

- Local storage
- Cloud storage
- Archive

---

# Operational Entities

These entities record activity.

---

## Publishing Job

Represents one publishing operation.

---

## Analytics Record

Represents collected performance metrics.

---

## Learning Record

Represents an approved learning observation.

---

## Audit Record

Represents a significant platform event.

---

## Error Record

Represents a recorded execution failure.

---

## Provider Benchmark

Represents measured provider performance.

Examples

- Cost
- Speed
- Quality
- Reliability

---

# Entity Relationships

High-level relationships are shown below.

```
Channel
    │
    ▼
Channel Package
    │
    ▼
Workflow
    │
    ▼
Execution
    │
    ▼
Execution Context
    │
    ▼
Stage
    │
    ▼
Task
    │
    ▼
Agent
    │
    ▼
Skill
```

Content produced during execution

```
Topic
    ↓
Research
    ↓
Script
    ↓
Voice
    ↓
Visuals
    ↓
Episode
    ↓
Publishing Job
    ↓
Analytics Record
    ↓
Learning Record
```

---

# Entity Ownership

Every Entity has exactly one owner.

Examples

Channel
→ Channel Package

Workflow
→ Core

Service
→ Core

Skill
→ Skill Registry

Agent
→ Runtime

Execution
→ Runtime

Analytics Record
→ Analytics Service

Learning Record
→ Learning Service

Asset
→ Asset Management

Ownership must always be explicit.

---

# Persistence

Entities are classified as either:

Persistent

Examples

- Channel
- Skill
- Episode
- Asset
- Analytics
- Learning

Temporary

Examples

- Execution
- Agent
- Task Context
- Runtime State

Temporary entities must never outlive execution.

---

# Identity

Every persistent Entity must have:

- Stable identifier
- Version (when applicable)
- Owner
- Lifecycle
- Status

Identity is independent of implementation.

---

# Extensibility

New Entities may be introduced without modifying existing Entity definitions.

Every new Entity must:

- Have a unique purpose
- Have one owner
- Belong to one category
- Follow lifecycle rules

---

# Authority

This document defines the canonical vocabulary of Cortex.

Every document, Service, API, Workflow, and implementation must use these Entity definitions consistently.

No implementation should introduce a new persistent Entity without first updating this document.