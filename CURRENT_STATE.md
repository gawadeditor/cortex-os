# CURRENT_STATE.md

# Cortex Current State

Version: 1.0

Status: Living Document

Authority: High

---

# Purpose

This document describes the current implementation state of Cortex.

Unlike the Architecture and Roadmap documents, this file changes continuously.

Its purpose is to answer one question:

**Where are we today?**

---

# Current Development Stage

Current Phase

Late Phase 2 — exit criteria met, hardening before advancing to Phase 3

Roadmap Progress

Foundation → Largely Complete

Autonomous Production → Operational, validated end-to-end

Autonomous Runtime → Core objectives met, monitoring and recovery hardening in progress

Analytics → Partial

Learning → Early Stage

Multi-Channel → Planned

Commercial Platform → Planned

Documentation → Synchronized with implementation as of 2026-07-25 (Cortex
Constitution adopted, Architecture Alignment Report reviewed, ADR-008
recorded for Service Layer convergence). Documentation work is closed
until implementation materially changes reality.

---

# Completed Foundations

The following platform foundations are considered operational.

## Core

- Runtime framework, including a validated Minimum Autonomous Runtime
  (launchd-supervised worker, live-tested against a hard process kill with
  full before/after evidence)
- Orchestration framework
- Multi-agent workflow
- Configuration system
- Secret management, including a single canonical runtime source of truth
  for OAuth-rotatable credentials (`secret_profiles`, `.env` as bootstrap
  fallback only)
- Provider abstraction
- Execution pipeline

---

## Production Pipeline

Operational stages include:

- Topic discovery
- Research
- Verification
- Narrative structure
- Script generation
- Script review
- Script revision
- Voice generation
- Visual planning
- Thumbnail generation
- Assembly
- Publishing pipeline

---

## Skills

The Skills framework exists.

Core production components are being migrated toward reusable Skills.

Skills are becoming the preferred mechanism for reusable expertise.

---

## Provider Layer

Provider abstraction exists.

Current integrations include multiple AI providers across text, image, video, and voice generation.

Provider selection is partially automated.

Benchmarking remains incomplete.

---

# Partially Complete Systems

The following systems exist but are not yet considered production complete.

## Autonomous Runtime

Status

Core objectives met. A complete unattended production cycle has been
executed and verified live: Discovery → Queue → Scheduler → Production →
Publishing → Analytics, with exactly-once private YouTube upload
confirmed against the live YouTube API (not database evidence alone), and
the worker returning to idle afterward. Two defects that blocked this
were found and fixed this cycle: orphaned `run_queue`/`stage_executions`
state after a worker crash never reconciling (queue and stage
reconciliation now extended alongside the existing `runs`-table
reconciliation), and a scheduler-derived `target_publish_at` written
keyed by `topic_id` but read keyed by `episode_id` for organically-queued
topics (now persisted at claim time with a passive regression guard).

Achieved

- Continuous execution (validated live via the launchd-supervised worker)
- Background scheduling (validated live end-to-end)
- Long-running worker reliability (validated against a real hard-kill)
- Baseline recovery from failures (orphaned queue/stage/run state
  reconciles to a terminal failed state on next worker startup; recovery
  is manual re-run via the existing dashboard endpoint, not automatic
  retry)

Remaining objectives (current priority — see Current Priorities below)

- Runtime supervision / monitoring: no proactive alerting exists yet on
  stuck runs, repeated failures, or worker health beyond what the
  dashboard surfaces on request.
- Recovery hardening: reconciliation is terminal-only by design (no
  auto-retry). A confirmed, documented-but-unfixed gap remains: neither
  `hypogeum/pipeline.py` (`_record_stage_skipped()` exists but is never
  called) nor `hypogeum/agents/publishing_agent.py` (no
  `publishing_outcomes` check before upload) implements checkpoint-skip or
  publish-idempotency, so a rerun after a crash that happens after a real
  publish succeeds but before its run/queue rows are marked complete would
  re-publish. Not yet in scope for a fix.

---

## Analytics

Status

Partial

Current capabilities

- Publishing metrics
- Platform integrations
- Performance collection

Remaining work

- Cross-platform comparison
- KPI dashboards
- Recommendation engine
- Automated optimization

---

## Learning

Status

Early

Existing

- Initial learning structure
- Evidence collection

Remaining

- Factory learning
- Channel learning
- Prompt learning
- Provider optimization
- Thumbnail optimization
- Audience learning

---

# Planned Systems

The following major systems are intentionally deferred.

- Multi-channel execution
- Organization management
- White-label deployment
- Marketplace
- Public SDK
- Public API
- Billing
- Licensing

---

# Current Priorities

The current implementation priorities are:

1. Complete reliable autonomous runtime.

2. Achieve unattended end-to-end production.

3. Strengthen runtime monitoring and recovery.

4. Complete analytics feedback loops.

5. Expand learning systems.

No new strategic feature work should bypass these priorities unless it directly removes a blocker.

---

# Known Constraints

Current development is intentionally focused on platform stability rather than feature expansion.

The objective is to establish a reliable autonomous operating platform before introducing advanced commercial capabilities.

---

# Success Criteria

The current phase is complete when Cortex can:

- Discover opportunities autonomously. — Achieved, validated live.
- Produce complete media assets. — Achieved, validated live.
- Publish without routine human intervention. — Achieved, validated live
  against the real YouTube API, exactly once.
- Monitor execution continuously. — Not yet. Current priority.
- Recover from failures automatically. — Partial. Orphaned state
  reconciles to a safe terminal state; recovery itself (re-run) is
  triggered manually, not automatically. Current priority.
- Use collected analytics to improve future executions. — Not yet.
  Analytics feedback loop is Phase 3, not started.

---

# Living Document

This document should be updated whenever the implementation state changes significantly.

It reflects reality, not intention.

The Roadmap defines where Cortex is going.

This document defines where Cortex is today.

---

# Authority

This document is the official snapshot of Cortex implementation progress.

If this document conflicts with implementation,

the implementation should be corrected,

or this document should be updated to reflect reality.

Reality always takes precedence over assumptions.