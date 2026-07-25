# FACTORY_PHILOSOPHY.md

# Cortex Philosophy

Version: 1.0
Status: Canonical

---

# Purpose

This document defines the immutable principles that govern Cortex.

These principles are architectural constraints.

No feature, agent, workflow, or future redesign may violate them.

If implementation conflicts with philosophy,

the implementation must change.

Never the philosophy.

---

# Principle 1

Cortex is an Operating System.

It is not a workflow.

It is not a collection of AI prompts.

It is not a dashboard.

It is not a YouTube automation tool.

Everything inside Cortex exists to operate autonomous organizations.

---

# Principle 2

Channels are Products.

Cortex is Infrastructure.

Channels are customers of Cortex.

They are not part of Cortex itself.

Replacing one channel should never require changing Cortex.

---

# Principle 3

Identity never exists inside Cortex.

Cortex never knows:

- channel name
- niche
- audience
- tone
- logo
- prompts
- publishing style
- thumbnail style

Everything belongs to the Channel Package.

---

# Principle 4

Execution is Stateless.

Each execution starts from a clean runtime.

Persistent information must be loaded explicitly.

Nothing survives accidentally between executions.

---

# Principle 5

Configuration is Data.

Behavior must be controlled through configuration whenever possible.

Never through hardcoded values.

Examples:

Good:

Channel Profile
Provider Profile
Publishing Policy
Privacy Policy

Bad:

if channel == "Hypogeum"

---

# Principle 6

Skills contain expertise.

Agents contain responsibility.

Runtime contains execution.

These responsibilities never overlap.

Agent

decides WHAT.

Skill

knows HOW.

Runtime

decides WHEN.

---

# Principle 7

Every decision must be explainable.

For every autonomous action,

the system should be able to answer:

Why?

What evidence?

What alternatives existed?

Why was this chosen?

---

# Principle 8

Optimization never sacrifices correctness.

Priority:

Correct

↓

Reliable

↓

Repeatable

↓

Efficient

↓

Cheap

Never reverse this order.

---

# Principle 9

Automation first.

Manual actions are exceptions.

If humans repeatedly perform the same task,

the platform should eventually automate it.

---

# Principle 10

Humans define intent.

Cortex determines execution.

Example:

Human:

Publish one documentary every week.

Cortex decides:

topic

schedule

providers

generation

publishing

analytics

iteration

---

# Principle 11

Evidence over opinion.

Every optimization should come from:

analytics

measurements

cost

quality

retention

CTR

success rate

failure rate

Never optimize because someone "feels" it is better.

---

# Principle 12

Learning never stops.

Every execution produces information.

Every information becomes knowledge.

Every knowledge influences future decisions.

Execution is the training loop.

---

# Principle 13

Factory Learning and Channel Learning are independent.

Factory Learning improves Cortex.

Channel Learning improves one channel.

Neither contaminates the other.

---

# Principle 14

Every component has one owner.

Never duplicate responsibility.

Examples

Publishing

Owner:

Publishing System

NOT

Dashboard

NOT

Scheduler

NOT

Channel Package

---

# Principle 15

The Dashboard is replaceable.

Deleting the Dashboard should never stop Cortex.

Deleting Cortex makes the Dashboard useless.

This defines ownership.

---

# Principle 16

Every capability should be reusable.

Nothing should exist only because Hypogeum needs it.

If a feature cannot support another future channel,

its design should be reconsidered.

---

# Principle 17

Failures are expected.

Systems must recover automatically whenever possible.

Retry.

Fallback.

Resume.

Repair.

Only escalate when recovery fails.

---

# Principle 18

Safety before autonomy.

Autonomous systems must never perform irreversible actions without satisfying safety policies.

Examples:

Publishing

Deletion

Financial operations

Credential rotation

---

# Principle 19

Everything measurable should be measured.

Examples:

Latency

Cost

Failure rate

Retry count

Token usage

Render duration

Upload duration

CTR

Retention

Completion rate

Nothing important should remain invisible.

---

# Principle 20

Every system should improve itself.

Improvement is part of execution.

Not a future phase.

---

# Principle 21

Architecture survives technology.

Providers change.

Models change.

APIs change.

The architecture should not.

Cortex must outlive individual AI models.

---

# Principle 22

Replaceability is mandatory.

Every external dependency must be replaceable.

Providers.

Models.

Storage.

LLMs.

Rendering engines.

Publishing APIs.

No vendor lock-in.

---

# Principle 23

Single Source of Truth.

Every concept has one canonical owner.

Examples

Identity

↓

Channel Package

Scheduling Policy

↓

Scheduling System

OAuth

↓

Secret Profiles

Capability Status

↓

Master Capability Blueprint

Current Progress

↓

Current State

Never duplicate authoritative information.

---

# Principle 24

Build platforms.

Not projects.

Hypogeum validates Cortex.

Hypogeum is not Cortex.

Future channels must require configuration,

not redevelopment.

---

# Final Principle

Cortex exists to build autonomous media organizations.

Every architectural decision should move the platform closer to requiring less human intervention while producing higher quality outcomes.