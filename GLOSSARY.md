# GLOSSARY.md

# Cortex Glossary

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines the official terminology used throughout Cortex.

Every architectural document, specification, implementation, and discussion must use these definitions consistently.

If a term is defined here, its meaning must not change elsewhere.

---

# General Principles

- Every term has exactly one official meaning.
- Different terms must not describe the same concept.
- One term must not have multiple meanings.
- Definitions are implementation-independent.
- This glossary is the authoritative vocabulary of Cortex.

---

# A

## Agent

A temporary runtime execution unit responsible for performing one or more Tasks on behalf of a Service.

Agents do not own business logic.

Agents do not own knowledge.

Agents exist only during execution.

---

## Analytics

The collection, processing, and interpretation of measurable performance data.

Analytics provide evidence for Learning.

---

## Asset

Any reusable artifact produced or consumed by Cortex.

Examples include:

- Images
- Video
- Audio
- Scripts
- Prompts
- Metadata
- Documents

---

# C

## Capability

A business function that Cortex can perform.

Capabilities describe **what** the platform can do.

They do not describe implementation.

---

## Channel

A media organization operated by Cortex.

A Channel is defined by its Channel Package.

Examples:

- Hypogeum
- Science Weekly
- Finance Daily

---

## Channel Learning

Knowledge accumulated specifically for one Channel.

It is never shared across Channels.

---

## Channel Package

The complete definition of a Channel.

It contains identity, objectives, editorial DNA, policies, learning, and configuration.

It never modifies Cortex itself.

---

## Configuration

Structured information used to customize behavior without changing code.

---

## Cortex Core

The central coordination layer of Cortex.

The Core manages execution, orchestration, policies, and platform behavior.

---

# D

## Decision Log

The official historical record of architectural decisions.

---

## Dependency

A required relationship between components.

Dependencies should always follow the architectural rules defined by Cortex.

---

# E

## Entity

A canonical concept recognized by the Cortex Data Model.

Entities represent things the platform understands.

---

## Execution

A single runtime instance performing work.

Execution begins with a request and ends when cleanup is complete.

---

## Execution Context

The temporary collection of runtime information required during execution.

Destroyed after execution completes.

---

# F

## Factory Learning

Knowledge accumulated by Cortex about platform operation.

Examples:

- Provider performance
- Runtime optimization
- Cost optimization

Factory Learning is shared across all Channels.

---

# G

## Governance

The rules that control how Cortex evolves.

Governance ensures architectural consistency.

---

# I

## Infrastructure

Shared technical resources supporting execution.

Examples:

- Storage
- Databases
- Providers
- Secrets
- Logging

Infrastructure never owns business logic.

---

# L

## Learning

The process of improving future execution using historical evidence.

Learning may update Factory Learning or Channel Learning.

---

# P

## Policy

A rule that constrains or governs execution.

Examples:

- Retry limits
- Budget limits
- Publishing restrictions

---

## Provider

An external system that supplies capabilities to Cortex.

Examples:

- Text generation
- Video generation
- Voice generation

---

# S

## Service

The primary business unit of Cortex.

Services implement business capabilities.

Services own business logic.

---

## Skill

A reusable package of knowledge or methodology.

Skills enhance Services.

Skills do not execute work independently.

---

## Stage

A logical phase within a Workflow.

Each Stage invokes one or more Services.

---

# T

## Task

The smallest executable unit of work.

Tasks are executed by Agents.

---

# V

## Validation

The process of ensuring that inputs, outputs, or configurations satisfy defined rules before execution continues.

---

# W

## Workflow

An ordered execution plan coordinating multiple Services to achieve a business objective.

A Workflow defines orchestration.

It does not own business logic.

---

# Relationships

The following hierarchy summarizes the core terminology:

Platform

↓

Channel

↓

Channel Package

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

---

# Naming Rules

The following naming conventions apply throughout Cortex.

- Use "Channel Package", never "Project".
- Use "Workflow", never "Pipeline" when referring to business orchestration.
- Use "Service", never "Module" for business domains.
- Use "Skill", never "Template" for reusable expertise.
- Use "Agent", never "Worker" when referring to runtime execution units.
- Use "Execution", never "Run" in architectural documentation.
- Use "Execution Context", never "Session" for temporary runtime state.

Implementation may use different internal names, but architectural documentation must use the canonical terminology defined here.

---

# Authority

This document defines the official vocabulary of Cortex.

If terminology conflicts arise between documents, the definitions in this glossary take precedence.