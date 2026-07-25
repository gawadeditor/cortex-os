# SKILLS_SPEC.md

# Cortex Skills Specification

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the official Skills architecture of Cortex.

Skills are the primary mechanism for extending the platform with reusable expertise.

A Skill represents knowledge, methodology, or specialized behavior.

Skills do not control execution.

Skills do not own business domains.

Skills enhance Services.

---

# Core Principle

Services perform work.

Skills provide expertise.

The Cortex Core coordinates execution.

---

# Definition

A Skill is a reusable package of knowledge that can be applied by one or more Services during execution.

A Skill answers the question:

"How should this task be performed?"

rather than

"What task should be performed?"

---

# Responsibilities

A Skill may provide:

- Domain knowledge
- Best practices
- Decision frameworks
- Prompt templates
- Validation rules
- Style guides
- Optimization strategies
- Reusable methodologies

A Skill never owns execution.

---

# Design Principles

## Reusable

A Skill should be usable by multiple Services.

Knowledge should never be duplicated.

---

## Independent

A Skill must not depend on a specific Channel Package.

Editorial identity belongs elsewhere.

---

## Replaceable

A Skill can be replaced without changing the Service that uses it.

---

## Composable

Multiple Skills may be applied during the same task.

Skills should complement each other rather than compete.

---

## Versioned

Every Skill has an independent version.

Updating a Skill should not require modifying Cortex.

---

# Examples

Examples include:

- Documentary Writing
- Investigative Research
- SEO Optimization
- Thumbnail Strategy
- Humanization
- Prompt Engineering
- Historical Verification
- Visual Storytelling
- Cinematic Composition
- Color Theory

These Skills may be shared across many Services.

---

# Skill Categories

## Editorial Skills

Knowledge related to writing and storytelling.

Examples

- Narrative Design
- Documentary Writing
- Journalism
- Humanization

---

## Visual Skills

Knowledge related to images and video.

Examples

- Composition
- Lighting
- Camera Language
- Thumbnail Design
- Color Grading

---

## Technical Skills

Knowledge related to production.

Examples

- Prompt Engineering
- Provider Optimization
- Video Generation
- Audio Generation

---

## Analytical Skills

Knowledge related to evaluation.

Examples

- SEO
- CTR Analysis
- Audience Psychology
- Performance Optimization

---

## Platform Skills

Knowledge related to Cortex itself.

Examples

- Provider Selection
- Cost Optimization
- Runtime Optimization
- Reliability

---

# Skill Lifecycle

A Skill follows this lifecycle.

```
Load

↓

Validate Compatibility

↓

Apply Knowledge

↓

Return Recommendations

↓

Unload
```

Skills are loaded only when required.

---

# Interaction Model

```
Core

↓

Service

↓

Skill

↓

Result
```

The Core selects the Service.

The Service selects the appropriate Skills.

Skills never invoke Services.

Skills never invoke the Core.

---

# Inputs

A Skill may receive:

- Execution Context
- Task Context
- Objectives
- Policies
- Inputs provided by the Service

Skills should only receive the information necessary for their purpose.

---

# Outputs

A Skill returns structured knowledge.

Examples

- Recommendations
- Prompt fragments
- Validation feedback
- Scoring
- Strategies
- Structured guidance

A Skill does not execute actions directly.

---

# Channel Independence

Skills should remain channel-agnostic.

They should not contain:

- Brand identity
- Editorial mission
- Audience definitions
- Publishing schedules

Those belong to the Channel Package.

---

# Discovery

Skills are registered with Cortex.

Services discover Skills through the Skill Registry.

Skills should never be hardcoded into Services.

---

# Versioning

Every Skill must define:

- Name
- Version
- Compatibility
- Dependencies
- Capabilities provided

---

# Observability

Every Skill should expose:

- Version
- Execution time
- Success
- Failure
- Usage statistics

This enables optimization over time.

---

# Governance

Skills should be:

- Reviewed
- Versioned
- Documented
- Reusable
- Testable

Deprecated Skills remain available until safely removed.

---

# Relationship to Other Components

Core

Coordinates execution.

Services

Own business domains.

Skills

Provide expertise.

Agents

Execute work using Services and Skills.

Channel Packages

Provide identity and objectives.

---

# Authority

This document defines the official Skill architecture of Cortex.

Reusable knowledge should be implemented as Skills whenever practical.

No Service should duplicate expertise that can be shared through a Skill.