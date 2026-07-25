# GOVERNANCE.md

# Cortex Governance

Version: 1.0

Status: Canonical

Authority: Highest

---

# Purpose

This document defines how Cortex evolves.

It establishes the governance rules for architecture, capabilities, documentation, and implementation.

Its purpose is to preserve consistency over the lifetime of the platform.

---

# Core Principle

Architecture is intentional.

Evolution is controlled.

No significant change should occur without following the governance defined in this document.

---

# Governance Objectives

Cortex governance exists to ensure:

- Architectural consistency
- Controlled evolution
- Long-term maintainability
- Predictable development
- Clear ownership
- Stable interfaces

---

# Documentation Hierarchy

The official documentation hierarchy is:

1. PROJECT_NORTH_STAR
2. FACTORY_PHILOSOPHY
3. MASTER_CAPABILITY_BLUEPRINT
4. CORTEX_ARCHITECTURE
5. ROADMAP
6. CURRENT_STATE
7. DECISION_LOG
8. CHANNEL_PACKAGE_SPEC
9. EXECUTION_MODEL
10. SERVICE_MODEL
11. SKILLS_SPEC
12. AGENT_MODEL
13. WORKFLOW_MODEL
14. DATA_MODEL
15. GOVERNANCE

Lower documents must never contradict higher documents.

---

# Authority Order

If two documents disagree:

North Star

↓

Philosophy

↓

Architecture

↓

Capability Blueprint

↓

Specifications

↓

Implementation

The higher authority always prevails.

---

# Architectural Decisions

Every architectural change must satisfy at least one of the following:

- Introduces a new capability.
- Improves an existing capability.
- Removes unnecessary complexity.
- Resolves an architectural conflict.
- Enables future scalability.

Otherwise, the change should not be accepted.

---

# Capability Changes

A new capability may only be introduced after:

1. Updating the Capability Blueprint.
2. Confirming architectural compatibility.
3. Recording the decision in the Decision Log.

Implementation follows documentation.

Never the reverse.

---

# Breaking Changes

Breaking changes are permitted only when:

- The long-term architectural benefit is significant.
- Migration is feasible.
- Existing behavior cannot reasonably be preserved.

Every breaking change requires an ADR entry.

---

# Versioning

Every canonical document has its own version.

Major version

Breaking architectural changes.

Minor version

New concepts.

Patch version

Clarifications.

---

# Deprecation

Nothing is removed immediately.

Lifecycle:

Proposed

↓

Accepted

↓

Deprecated

↓

Archived

Historical records are preserved.

---

# Ownership

Every architectural concept must have one owner.

Examples:

Capabilities

→ Capability Blueprint

Architecture

→ Architecture document

Execution

→ Execution Model

Skills

→ Skills Specification

Workflows

→ Workflow Model

Ownership must be explicit.

---

# Change Process

Every significant change follows this sequence:

Identify Need

↓

Evaluate Architecture

↓

Update Documentation

↓

Record Decision

↓

Implement

↓

Validate

Documentation always precedes implementation.

---

# Review Principles

Every proposal should be evaluated for:

- Simplicity
- Reusability
- Scalability
- Maintainability
- Independence
- Replaceability

---

# Success Criteria

Good governance produces:

- Stable architecture
- Predictable evolution
- Minimal duplication
- Clear ownership
- Consistent terminology

---

# Authority

This document defines how Cortex evolves.

No architectural change should bypass the governance process defined here.