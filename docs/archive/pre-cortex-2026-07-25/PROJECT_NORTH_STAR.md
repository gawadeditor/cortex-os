# PROJECT NORTH STAR

**Version:** 1.0
**Status:** Long-term vision. Time-invariant — this document should rarely change. If a decision requires changing it, that is a signal to pause and confirm intent explicitly, not to edit it quietly.

This document answers one question only: **where is Hypogeum Brain ultimately going?** It does not contain current priorities, phase sequencing, or execution rules — those live in `PROJECT_CONSTITUTION.md`, `CURRENT_PHASE.md` / `CURRENT_PROGRESS.md`, and `EXECUTION_PROTOCOL.md` respectively. See "Relationship to Other Governance Documents" below.

---

## Vision Statement

Hypogeum Brain is a self-improving, multi-channel autonomous media factory **platform** — not a YouTube channel, not an episode-production tool, and not a single-purpose automation script. It discovers topics, produces documentary-style episodes end-to-end, publishes them, and learns from real performance data to improve future output, with progressively less human involvement per episode over time.

## Long-Term Product Definition

- **The factory is the product.** Episodes are validation artifacts that prove the factory works — not the deliverable being optimized for in isolation.
- **"The Hypogeum Chronicles" is the first template instance**, not the platform itself. Its brand, topic domain, and voice are one configuration of a general-purpose factory.
- **New channels are created by cloning the template** with a different brand/topic/identity configuration — the same pipeline, agents, and governance infrastructure serve every channel.
- **Multi-channel from the start, architecturally** — even while only one channel is active, no channel-specific assumption should be hardcoded into a shared module without a documented reason.

## End-State Capabilities

A mature Hypogeum Brain deployment:

- Runs an autonomous discover → produce → publish → learn loop across multiple channels concurrently, with minimal per-episode human intervention.
- Is self-improving: real analytics and performance data feed back into topic selection, narrative structure, thumbnail composition, and model/provider choice.
- Is cost-aware by construction: every provider call is tracked, reconciled against real billing, and factored into per-episode and per-channel economics — never estimated in isolation.
- Is quality-gated, not volume-gated: automation expands only after the corresponding manual/supervised process has proven consistent quality (see `PROJECT_PRINCIPLES.md` #2 and #4).
- Operates across multiple publishing platforms (YouTube, TikTok, Instagram, Facebook today; extensible to others) with platform-specific formatting handled by the pipeline, not by hand.
- Maintains long-term memory of its own output — what it has said, shown, and claimed — to avoid contradiction and repetition as the library of episodes grows.

## What This Is Not

- Not a single-channel YouTube automation tool.
- Not optimized for publishing speed or output volume as primary goals (`PROJECT_PRINCIPLES.md` #1, #2 — automation is forbidden until manual production consistently passes QA).
- Not a project where the dashboard, runtime, or any single subsystem is the objective — each is infrastructure in service of the factory (`PROJECT_PRINCIPLES.md` #5, #6).
- Not a project that treats "it generated an episode" as success — success is a self-improving, cost-reconciled, multi-channel factory that keeps working correctly as it scales.

## Relationship to Other Governance Documents

| Document | Answers |
|---|---|
| `PROJECT_NORTH_STAR.md` (this file) | Where is this ultimately going, and why? (rarely changes) |
| `PROJECT_CONSTITUTION.md` | What governs decisions in the *current* phase, and what is never allowed? |
| `PROJECT_PRINCIPLES.md` | What operating rules apply to every change, regardless of phase? |
| `docs/future-roadmap.md` (ROADMAP) | What is the sequenced, phased path from today to this vision? |
| `CURRENT_PHASE.md` | Which phase are we in right now, and what comes after it? |
| `CURRENT_PROGRESS.md` | What is done, active, and blocked *today*, in detail? |
| `EXECUTION_PROTOCOL.md` | How does implementation work actually get done, step by step? |
| `DECISION_LOG.md` | Why were specific past sequencing/scope decisions made? |

This document is an input to the Roadmap, not a substitute for it. Conversation history, model assumptions, and casual suggestions never override any document in this hierarchy (`PROJECT_CONSTITUTION.md`, "ROADMAP AUTHORITY").
