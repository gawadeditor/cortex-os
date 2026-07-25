# DECISION LOG

## 2026-07

Decision:
Factory quality is prioritized over episode production.

Reason:
Producing low-quality episodes wastes money and tokens.

-------------------------

Decision:
Runtime postponed until manual QA succeeds.

Reason:
Automation without quality validation is unacceptable.

-------------------------

Decision:
Graphify integrated before skills.

Reason:
Reduce token consumption before expanding skills.

-------------------------

Decision:
Skills redesigned before runtime.

Reason:
Architecture must stabilize first.

-------------------------

Decision:
Dashboard UX begins after production pipeline is validated.

Reason:
Avoid polishing UI before backend stability.

-------------------------

Decision:
Model evaluation framework becomes part of the factory.

Reason:
The system must continuously discover and upgrade the best models automatically.

-------------------------

Decision:
Humanizer is attached as a Script Engine skill, not a separate pipeline stage.

Reason:
Skills extend the owning agent's existing single LLM call instead of becoming a new stage, adapter, or checkpoint layer — this is now the standard for all future skills (GPT Image, Seedance, Veo Prompt Director).

-------------------------

Decision:
Dashboard Platform sequencing updated. Dashboard Platform is promoted ahead of Full Manual Episode Validation in the implementation sequence.

Supersedes:
The prior decision in this log stating that Dashboard UX begins after production pipeline is validated.

Reason:
Full Manual Episode Validation is currently blocked by an external infrastructure dependency (YouTube OAuth) that engineering work cannot resolve. Dashboard Platform development has no dependency on that blocker. Engineering work should therefore continue on the Dashboard while OAuth is being restored. Full Manual Episode Validation remains mandatory before declaring the factory production-ready — this change modifies implementation sequencing only, it does not reduce the production readiness requirements.

-------------------------

Decision:
Log isolation and rotation for pipeline.log is recorded as technical debt and deferred, not fixed now.

Reason:
pipeline.log has grown to approximately 24GB (unrotated, polluted by test writes), which materially slowed debugging during the 2026-07-19 validation campaign (a grep against it timed out). It is not a hard blocker to any current workstream — Dashboard Platform and Full Manual Episode Validation do not depend on it — so it is deferred rather than fixed opportunistically. Tracked in CURRENT_PROGRESS.md and docs/future-roadmap.md as an open item within the Architecture Stabilization phase.

-------------------------

Decision:
Minimum Operational Dashboard, Phase 2 (Gate Attempt panel + Smoke Test control) is architecturally validated: the dashboard remains a thin observability/control layer over Factory OS and owns no runtime behavior.

Reason:
A self-review against the project North Star (Factory OS is the product; the dashboard is only an operational window into it) confirmed the following, each checked against the actual implementation rather than assumed:
- The Smoke Test endpoint (/api/worker/smoke-test) is a pure wrapper over the pre-existing hypogeum.runtime.smoke_run() — verified via git history (hypogeum/runtime.py predates every commit made in this workstream, including the first one reviewed). The route contains no logic beyond calling that function and reformatting its already-returned SmokeResult fields into JSON.
- The dashboard owns no orchestration: neither Phase 2 addition calls into hypogeum.queue, hypogeum.scheduler, or hypogeum.pipeline directly, and neither introduces a code path capable of reaching pipeline.run_pipeline().
- dry_run safety is enforced inside Factory OS, not the dashboard: smoke_run() hardcodes dry_run=True at its internal poll() call site as a literal, not a parameter — there is no argument the dashboard (or any caller) can pass to override it. The only way this could change is by editing hypogeum/runtime.py itself, which is Factory OS core, not dashboard code.
- hypogeum.worker_integration.WorkerAttempt (via load_latest_worker_attempt()) is treated as the canonical source of gate history. The new Gate Attempt panel on /run/<id> passes the WorkerAttempt object straight through to the template with no DTO copy and no reinterpretation — it reads the same artifact worker_integration.py itself persists and reads elsewhere.
- Recovery classification (the pre-existing failure_class system covering pipeline-execution failures: resumable/finance_gate/retryable/credential/config/fatal) and gate history (WorkerAttempt.blocked_reason, covering pre-execution gate refusals that never reach run_pipeline()) are intentionally kept as separate concepts on the run detail page, not merged into one classification. They answer different questions — "why did the pipeline fail" vs. "was this episode ever blocked before the pipeline started" — and merging them would require inventing a new taxonomy neither system currently has.

-------------------------

Decision:
Repeated Smoke Test runs leaving terminal diagnostic rows (schedules/scheduled_runs/run_queue) behind is accepted as technical debt, not fixed now.

Reason:
hypogeum.runtime.smoke_run() creates a real, manual-cadence, factory-scoped schedule plus its scheduled_run and run_queue rows on every invocation; these reach terminal status (schedule next_run_at cleared, scheduled_run triggered, queue item completed) and are never reclaimed. This causes no functional interference with autonomous operation — terminal rows are never re-picked-up by tick()/consume_scheduled_runs()/claim_next() — only cosmetic DB accumulation if the control is used repeatedly. This is a pre-existing characteristic of smoke_run() itself (unchanged by the Minimum Operational Dashboard work, which only added a thin route wrapper around it), not something introduced by the dashboard. Deferred to a future runtime-cleanup milestone rather than fixed opportunistically, consistent with how the pipeline.log rotation debt above is already tracked.

-------------------------

Decision:
H1 and H2, the two defects confirmed by live MAR Production Validation (2026-07-25), are fixed and independently revalidated. MAR's queue/stage reconciliation and the scheduler's target_publish_at identity contract are corrected.

Reason:
H1: a run_queue row claimed at the moment its owning worker was hard-killed stayed permanently status='claimed', and its stage_executions row stayed 'running' forever — startup reconciliation (hypogeum/orphan_runs.py::reconcile_orphaned_runs()) only ever fixed the runs table. Fixed by extending that same function to reconcile run_queue and stage_executions in two tiers: Tier 1 attributes reconciled rows to the specific orphaned runs already identified (via episode_id — precise evidence over a blanket status sweep, per review); Tier 2 is a residual safety net for a worker crashing after claiming but before its runs row is registered. Both reach a terminal 'failed' state, not an automatic re-queue — recovery already exists and is already tested via the Re-run Episode path. Commit ab9cdb6.

H2: queue.py::process_next() derived and persisted the scheduler's target_publish_at keyed by topic_id, but worker_integration.py::lookup_episode_target_publish_at() read it back keyed by episode_id. For any topic without a pre-assigned episode_id — the default case for organically-queued/discovered topics, not just a test convenience — the freshly generated episode_id was never written back to episode_topics.episode_id, so the read always missed and every scheduled invocation against such a topic failed before the pipeline ever started. Fixed by persisting the assigned episode_id onto both episode_topics.episode_id and run_queue.episode_id at claim time (topics.set_topic_episode_id, db.set_queue_item_episode_id — structural twins of the existing set_topic_target_publish_at pattern), plus a passive regression guard that logs a structured warning if the identity contract ever fails to resolve again. Mode-agnostic, no schema change. Commit 1f8c52f.

Both fixes were revalidated live under real MAR supervision, not just unit tests: H1 by killing the worker mid-stage and confirming run_queue/stage_executions both reached a consistent terminal state with a single, non-duplicated reconciliation event; H2 by letting the real scheduler tick, promote, and claim an organically-queued topic with no pre-assigned episode_id, producing a WorkerAttempt record with blocked_reason=null and plan_status="executable" — the exact failure this defect previously produced is confirmed gone. Full evidence: docs/operations/MAR_PRODUCTION_VALIDATION_REPORT.md's Revalidation section.

Explicitly not touched in this workstream: hypogeum/agents/publishing_agent.py. Root-cause analysis surfaced a separate, pre-existing, confirmed gap directly adjacent to H1's "must never publish twice" requirement — hypogeum/pipeline.py has no real checkpoint-skip *decision* wired for a fresh claim path the way this repair needed it (the DB-side skip bookkeeping helper exists but a rerun still re-executes every stage including Publishing), and publishing_agent.py never checks publishing_outcomes before uploading — so a rerun after a crash that happened after a real publish succeeded but before the run/queue rows were marked complete could still genuinely re-publish. This is documented only, deferred to a future "execution idempotency" milestone (stage checkpointing, publishing idempotency, rerun semantics, checkpoint-skip behavior, dashboard consistency of the Re-run Episode endpoint's own claim that "only incomplete stages will re-run") — not fixed opportunistically inside MAR Production Repair, per explicit scope discipline.

One blocker was encountered and resolved operationally, not with a code change, during the first revalidation attempt: a new test topic collided in claim priority with an unconsumed stale topic left over from the original validation, causing the wrong (old) episode to be resumed via real on-disk checkpoint-skip logic. Classified as an environment/test-data artifact, confirmed independent of H1/H2 by direct evidence, and resolved by using a uniquely-namespaced topic with a priority (-1000000) that cannot collide with any historical validation topic — historical topics were left in place, not deleted.
