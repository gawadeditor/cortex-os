Current Phase:

Architecture Stabilization (Phase 1 of 7 — see docs/future-roadmap.md's Phase Sequence)

Last updated: 2026-07-25. Authoritative for which phase the project is in and what follows it. Full sequenced detail: docs/future-roadmap.md. Full status detail: CURRENT_PROGRESS.md. Vision: PROJECT_NORTH_STAR.md. How work gets done: EXECUTION_PROTOCOL.md.

Active Workstream:
MAR (Minimum Autonomous Runtime) Production Repair — complete as of 2026-07-25. MAR was built, then validated live (MAR Production Validation, 2026-07-25), which confirmed two real defects: H1 (a run_queue row claimed at the moment its worker was hard-killed stayed permanently claimed, with its stage_executions row stuck 'running' forever — startup reconciliation only ever fixed the runs table) and H2 (the scheduler-derived target_publish_at was written keyed by topic_id but read keyed by episode_id, so every scheduled invocation against an organically-queued topic failed before the pipeline started). Both were fixed (commits ab9cdb6, 1f8c52f) and independently reconfirmed live under real MAR supervision (kill-mid-stage recovery for H1; a real scheduler tick reaching an executable BackwardPlan with no blocked_reason for H2) — see docs/operations/MAR_PRODUCTION_VALIDATION_REPORT.md's Revalidation section for full evidence. H1/H2 are no longer blockers to autonomous operation; the sole remaining blocker is the pre-existing, externally-tracked YouTube OAuth failure below. Full Factory Runtime (the platform-independent architecture beyond MAR) remains explicitly deferred, not started.

Dashboard Platform. Governance & Roadmap Synchronization (the governance layer: PROJECT_NORTH_STAR.md, docs/future-roadmap.md, CURRENT_PROGRESS.md, EXECUTION_PROTOCOL.md, this document) is complete as of 2026-07-19 and approved. Dashboard Platform was promoted ahead of Full Manual Episode Validation in the phase sequence 2026-07-19 — see Sequencing Rationale in docs/future-roadmap.md.

Completed (see CURRENT_PROGRESS.md for full detail):
✓ MAR (Minimum Autonomous Runtime) — built, validated, H1/H2 defects repaired and revalidated (2026-07-25)
✓ Architecture Audit
✓ Prompt rewrite
✓ Thumbnail redesign
✓ Provider comparison
✓ Brand identity
✓ Working-tree audit
✓ Prompt Compiler + Video DNA v1 architecture
✓ Graphify integration (corrected 2026-07-19 — was previously miscategorized as deferred/current-work; it is complete and in active use)
✓ Brand Bible v1.1 / Thumbnail DNA v1-v2 (frozen 2026-07-19)
✓ Music Architecture Option B (frozen 2026-07-19)
✓ Finance Controller, Asset Archive Agent, QA Agent, Compliance Agent, Knowledge Graph seed extraction, World Bible (all confirmed active/wired 2026-07-19 — were previously miscategorized as deferred)
✓ Production safety gate, budget-ceiling clearance gate
✓ Governance & Roadmap Synchronization (PROJECT_NORTH_STAR.md, docs/future-roadmap.md, CURRENT_PROGRESS.md, EXECUTION_PROTOCOL.md, this document — created/reconciled and approved 2026-07-19)

Blocked:
YouTube OAuth token refresh failing (HTTP 400) — blocks any real publish. Confirmed 2026-07-19; operator/infra re-authorization required, not a code fix. ("Claude weekly limit," the prior entry here, is stale and unconfirmed — removed.)

Remaining in This Phase (Architecture Stabilization):
- Platform Observability & Financial Intelligence (recorded, not started)
- Reuse Engine wiring (built and tested, zero callers — not integrated)
- Log rotation (technical debt)
- Dedicated validation-run authorization mode (technical debt)

Sequence After This Phase (per docs/future-roadmap.md's Phase Sequence — do not resequence here without updating that document too):

Next Milestone (concurrent with active workstream above):
Full Manual Episode Validation — mandatory before declaring production readiness; blocked (YouTube OAuth, above); no longer gates Dashboard Platform or other engineering work — see Sequencing Rationale in docs/future-roadmap.md

After Dashboard Platform and Full Manual Episode Validation both complete:
Runtime

After Runtime:
Learning Loop

After Learning Loop:
Model Evaluation Framework

After That:
SaaS
