# Scheduling Architecture Consolidation Reference

**Date:** 2026-06-14
**Type:** Consolidation reference — not a design document
**Status:** Approved. Introduces no new architectural decisions.
**Scope:** Self-hosted Factory OS V1

---

## 1. Purpose

This document consolidates the scheduling architecture that exists in fragmented form across nine approved Factory OS design documents. It introduces no new systems, no new entities beyond the Schedule record schema derived directly from existing field definitions, no new governance rules, and no new implementation requirements.

The scheduling audit (2026-06-14) classified the scheduling system as **C. Partially Documented but Architecturally Complete**: 11 of 15 required capabilities are explicitly designed in existing documents, 2 of 15 are designed by implication from CALOG channel lifecycle, and 2 of 15 are partially documented with all decisions already made. No missing capability requires a new architectural decision.

---

## 2. Classification

| Classification | Result |
|---|---|
| Scheduling system status | **Partially documented — architecturally complete** |
| New design required | **No** |
| New governance system required | **No** |
| Factory OS Design Complete (self-hosted V1) | **Yes** |

The scheduling system is not a missing component. It is a cross-cutting concern whose architecture is distributed across the governance stack, run model, channel model, and connector model — all of which are approved and complete.

---

## 3. Schedule Entity Reference Schema

Derived entirely from existing approved field definitions. Every field below has a named source. No field is new.

```
Schedule {
  schedule_id:      string         // "sch_<uuid>" — referenced in B7 Run Schema §3
  channel_id:       string         // from Channel Profile (B4.75) — schedule belongs to channel
  workflow_id:      string         // from B7 Run Schema §3 — workflow to execute
  workflow_version: string         // from B7 Run Schema §3 — "latest" or pinned semver

  // Schedule configuration — all fields from B4.75 §4 platform_configs
  // and B1 §3 publishing_rules
  cadence:          string         // e.g., "weekly" — B4.75 platform_configs.cadence
  day_of_week:      string | null  // e.g., "thursday" — B4.75 platform_configs.day_of_week
  time_of_day:      string | null  // e.g., "18:00 UTC" — B4.75 platform_configs.time_of_day
  platform:         string         // which platform's schedule config to use from B4.75

  // Runtime state — computed, not configured
  next_run_at:      timestamp      // next fire time derived from cadence + day + time
  last_run_id:      string | null  // foreign key to most recent RunRecord (B7 §3)
  last_run_at:      timestamp | null
}
```

**Field source index:**

| Field | Source document | Section |
|---|---|---|
| `schedule_id` | Run OS (B7) | §3 Run Schema — `schedule_id: "sch_<uuid>" \| null` |
| `channel_id` | Channel Definition System (B1) | §3 Channel Schema |
| `workflow_id` | Run OS (B7) | §3 Run Schema — required field |
| `workflow_version` | Run OS (B7) | §3 Run Schema — `workflow_version: "3.2.1"` |
| `cadence` | Channel Profile (B4.75) | §4 `platform_configs.cadence` e.g., "weekly" |
| `day_of_week` | Channel Profile (B4.75) | §4 `platform_configs.day_of_week` e.g., "thursday" |
| `time_of_day` | Channel Profile (B4.75) | §4 `platform_configs.time_of_day` e.g., "18:00 UTC" |
| `platform` | Channel Profile (B4.75) | §4 `platform_strategy.primary_platform` |
| `next_run_at` | Derived — no new decision | Computed from cadence + day_of_week + time_of_day |
| `last_run_id` | Run OS (B7) | §3 Run Schema — `run_id` foreign key |

---

## 4. Source-of-Truth Table — All 15 Scheduling Capabilities

| # | Capability | Status | Authoritative Source(s) |
|---|---|---|---|
| 1 | Schedule entity | Partially documented — fields derivable | B7 §3 (`schedule_id`), B4.75 §4 (`cadence`, `day_of_week`, `time_of_day`), B1 §3 (`publishing_rules`) |
| 2 | Schedule lifecycle states | Designed by implication | B5.18 CALOG — channel lifecycle IS the schedule lifecycle. Blocked/paused/archived channel controls all scheduled runs for that channel. B1: "pausing scheduled runs" on channel status change. |
| 3 | Trigger generation | Designed | B7 §5 — `scheduled` trigger type, full requirements. B5.17 §18.1 — `create_run(initiated_by="scheduler")`. B5.17 §6.1 — `system:scheduler` as recognized authority. |
| 4 | RunRequest creation | Designed | B5.17 §18.1 — `create_run(factory_id, channel_id, episode_id, operator_id=null, initiated_by="scheduler", run_budget_ceiling, notes)` |
| 5 | Run OS queue integration | Designed | B7 §5 — scheduled runs enter same queue as manual runs at priority=2. B5.17 §3.2 — "operator initiates run / scheduler" as `created → queued` trigger. |
| 6 | schedule_id linkage | Designed | B7 §3 — `schedule_id: "sch_<uuid>" \| null` in Run Schema. Set to the originating Schedule's ID for all scheduled runs; null for manual and event-based runs. |
| 7 | workflow_id / workflow_version linkage | Designed | B7 §3 — `workflow_id` and `workflow_version` are required fields in every Run Schema. |
| 8 | Channel state enforcement | Designed | B5.16 REG §3 — channel active state is a governing precondition for all execution. B5.18 CALOG — `get_channel_state()` is called by REG on every new_run clearance. B1 — channel status change "pausing scheduled runs". |
| 9 | Authorization rules | Designed | B5.17 §6.1 — `system:scheduler` = "Factory OS scheduler" in the authority table. B5.16 §2 — `new_run` context: "Operator initiates run; scheduler triggers run". B5.15a — "Factory OS (scheduled)" as authorized invoker for analytics and maintenance agents. |
| 10 | Conflict handling | Designed | B7 §5 — "If the scheduled slot cannot be filled (e.g., system busy, cost ceiling breached), the run is marked `planned` and retried at next available window; it is not silently skipped." |
| 11 | Slot reservation | Designed | B5.16 REG — the clearance system is the slot mechanism. A `new_run` clearance request from the scheduler goes through the same preflight checks as an operator-initiated run. A `deferred` response (B5.16 §7.2) carries `deferred_until: timestamp` — the scheduler retries at that time. A `blocked` response → run marked `planned`, retried next cadence window per #10. B7 §5: "manual runs bypass schedule slots" confirms that slots are tracked per-channel, per-cadence. |
| 12 | Pause/resume behavior | Designed | B1 — channel status change explicitly "pausing scheduled runs". B5.18 CALOG — channel `paused` or `blocked` state prevents new run creation (enforced by REG #8 above). No separate schedule-level pause mechanism is required; channel lifecycle is the pause authority. |
| 13 | Audit records | Designed | B7 §3 — `trigger_source: "scheduled"`, `trigger_actor: "scheduler"`, `trigger_reason: "weekly_episode_slot"` in Run Schema. B7 §10 — `run_queued` audit event captures `trigger_source`, `trigger_actor`, `config_hash`, `priority`. B5.17 §5 — `initiated_by: "operator" \| "scheduler" \| "re_run_trigger"` in RunRecord. |
| 14 | Dashboard visibility | Designed | B7 §12 — Run Queue Panel (all queued runs; shows trigger_source and schedule context). Run History Panel (filter by trigger_source: "scheduled"). Active Runs Panel (all running runs regardless of trigger). Run Costs Panel (cost analytics sliced by trigger_source). |
| 15 | OIS signal handling | Designed | B7 §15 — scheduler-triggered runs emit the same `run_signal` events to OIS as operator-triggered runs. B5.16 §7.2 — `deferred` clearance with `deferred_until` timestamp allows the scheduler to coordinate with REG's time-aware deferral model. B5.21 OISG — OIS blocking signals reach the scheduler via channel_blocked transitions (B5.18 §13.3 → CALOG → OIS → `channel_blocked` signal) which prevent new scheduled runs on that channel. |

---

## 5. REG as the Slot Reservation Mechanism

The slot reservation concept named in B7 §5 (`scheduled` trigger requires "slot reservation") is served by the existing REG clearance system. No dedicated slot reservation subsystem is required or defined.

**How it works:**

1. Scheduler fires at `next_run_at` for a given channel.
2. Scheduler calls Run OS `create_run(initiated_by="scheduler")`.
3. B5.17 RLG records the run in `created` state → immediately transitions to `queued`.
4. Run OS requests `new_run` clearance from B5.16 REG.
5. REG evaluates all preconditions (channel active, credentials, budget, no blocking signals).

**Clearance outcomes and scheduler behavior:**

| REG Decision | Meaning | Scheduler Action |
|---|---|---|
| `clear_to_execute` | Slot is available — run proceeds | Run advances to `running` |
| `deferred` (with `deferred_until`) | Slot exists but timing constraint (quiet hours, rate limit) | Scheduler re-requests clearance at `deferred_until` |
| `blocked` | Channel state prevents execution | Run marked `planned`; retried at next cadence window (B7 §5) |
| `requires_approval` | Approval gate is open | Run waits in `pending_approval`; Approval System callbacks resume it |

B7 §5 also establishes that "manual runs bypass schedule slots" — meaning that manual runs do not consume or reserve schedule slots; only scheduled runs with a valid `schedule_id` interact with the cadence tracking. This distinction is enforced at the trigger_source level, not through a separate reservation table.

**Authority:** B5.16 REG owns clearance decisions. The scheduler does not make clearance decisions.

---

## 6. CALOG as Schedule Pause/Resume/Termination Authority

The channel lifecycle (B5.18 CALOG) is the authority for all schedule pause, resume, and termination behavior. No separate schedule state machine exists.

| Channel State | Effect on Scheduled Runs |
|---|---|
| `active` | Scheduled runs fire normally at `next_run_at` |
| `paused` | No new runs may be created (B5.18 §4); in-progress runs continue to completion. Scheduler fires at `next_run_at` → REG clearance blocked → run marked `planned` → retried at next cadence window. |
| `blocked` | Same as `paused`. OIS blocking signal that triggered the block also signals the scheduler context (B5.21 OIS Governance). |
| `inactive` | No new runs may be created. Scheduler does not fire for inactive channels. |
| `archived` | Terminal. Scheduler is deregistered for the channel. All pending scheduled runs are cancelled. |

**Authority chain for operator-initiated schedule pause:**

```
Operator pauses channel → CALOG.pause_channel() → ChannelStateTransition recorded →
channel_paused signal → OIS → Dashboard notification →
Scheduler fires at next_run_at → REG blocked (channel not active) →
run marked planned → retried next window (if channel resumes before window)
```

Operator does not pause the "schedule" directly. Operator pauses the channel. The schedule follows.

**Source:** B1 §5 ("pausing scheduled runs" on channel status change), B5.18 §2.1, B5.18 §4.

---

## 7. RLG and B7 as RunRequest and Queue Integration Authority

**B5.17 Run Lifecycle Governance owns:**
- The `create_run()` API that the scheduler calls
- The `initiated_by` field that records scheduler-vs-operator origin
- The `system:scheduler` entry in the state machine authority table
- The `queued` state that all scheduled runs enter immediately after creation

**B7 Run Operating System owns:**
- The `scheduled` trigger type definition and its requirements
- The `schedule_id` field in the Run Schema
- The `planned` fallback state when a slot cannot be filled
- The run queue mechanics (priority, queue position, estimated start time)
- The Run Queue Panel in the dashboard

**Integration sequence:**

```
1. Scheduler fires at next_run_at
2. Scheduler → Run OS: create_run(initiated_by="scheduler", schedule_id="sch_xxx", ...)
3. RLG: creates RunRecord in `created` state; transitions to `queued`
4. Run OS: requests new_run clearance from REG
5. REG: evaluates preconditions → returns clearance decision
6. If clear_to_execute: RLG transitions queued → running
7. If blocked: Run OS records blocked_reason; run stays in `planned` per B7 §5
8. If deferred: Run OS schedules re-clearance at deferred_until
```

**Authority:** B5.17 RLG owns state transitions. B7 Run OS owns queue mechanics. Scheduler initiates; it does not decide.

---

## 8. Dashboard Visibility References

No new dashboard components are required. All scheduling visibility is served by existing Run OS dashboard panels defined in B7 §12.

| Dashboard Panel | Scheduling Content | Source |
|---|---|---|
| Run Queue Panel | All queued runs including scheduled; shows `trigger_source: "scheduled"` and `schedule_id`; shows `trigger_reason` (e.g., "weekly_episode_slot") | B7 §12 |
| Active Runs Panel | All active runs; shows `trigger_actor: "scheduler"` for scheduled runs | B7 §12 |
| Run History Panel | Filter by `trigger_source: "scheduled"`; shows full scheduled run history | B7 §12 |
| Run Costs Panel | Cost analytics sliced by `trigger_source`; cost per scheduled run vs. manual run | B7 §12 |
| Run Failures Panel | Failure analytics including scheduled run failures; failure classification by trigger source | B7 §12 |
| Channel Detail — Lifecycle Section | Channel state (active/paused/blocked) directly communicates schedule status; no separate schedule indicator needed | B5.18 §16.2 |

---

## 9. OIS Signal Handling References

Scheduler-triggered runs interact with OIS through the same signal paths as operator-triggered runs. No new OIS signal types are required.

| Event | Signal | OIS Path | Source |
|---|---|---|---|
| Scheduled run created | `run_queued` audit event | Run OS → OIS `run_signal` | B7 §10 |
| Channel blocked (budget/credential/system) | `channel_blocked` signal | CALOG → OIS → channel_blocked → scheduler detects via REG blocked clearance on next fire | B5.18 §13.1, B5.21 |
| Scheduled run fails permanently | `failed_permanent` alert | Run OS → Dashboard alert + OIS signal | B7 §9 |
| REG defers scheduled run | `run_signal(deferred)` | Run OS → OIS, with `deferred_until` in payload | B5.16 §7.2 |
| Slot missed (consecutive deferrals) | No new signal — surfaced via Run Failures Panel alert threshold | B7 §12 |

**Authority:** B5.21 OIS Governance owns signal lifecycle. B5.16 REG owns clearance signals. Scheduler reads OIS state indirectly through REG clearance decisions. Scheduler does not write to OIS directly.

---

## 10. V1 Implementation Note (Local Deployment)

Platform Connector Governance (B5.10) §18 specifies the V1 implementation approach directly:

> "In local-first V1, the Connector Governance layer operates as a single-factory, single-machine system. Scheduling uses the local machine's clock and process scheduler."

This confirms that the V1 scheduler is not a distributed system. It runs on the local machine using the operating system's scheduling mechanism (OS-level cron, Python scheduler, or equivalent). No cloud scheduler is required.

B5.10 §12.1 further distinguishes:
- **Native scheduling** (preferred): factory sends content to the platform with a `scheduled_publish_at` timestamp; the platform handles timed release. Supported by YouTube, TikTok, Instagram.
- **Factory scheduling** (fallback): factory waits until the scheduled time and initiates the publish. If the machine is unavailable at publish time, the publish is queued as pending on next startup and the operator is notified if the delay exceeds 2 hours.

These two modes address the publish-time scheduling concern. Episode production scheduling (when to run the episode pipeline) is handled by the scheduler mechanism described above.

---

## 11. No New Architectural Decisions

This document introduces:

- No new system
- No new governance layer
- No new approval path
- No new OIS signal type
- No new dashboard panel
- No new database table beyond the Schedule entity schema above, which is fully derived from existing approved field definitions
- No change to any existing document's design

Every fact in this document is a cross-reference to an approved source. Where a fact is "designed by implication," the implication is stated and the source documents are cited.

---

## 12. Document Cross-Reference Index

Complete list of source documents for the scheduling architecture.

| Document | Phase | Scheduling Contribution |
|---|---|---|
| Channel Definition System | B1 | `cadence`, `day_of_week` in `publishing_rules`; "pausing scheduled runs" on channel status change |
| Channel Profile System | B4.75 | `platform_configs.cadence`, `.day_of_week`, `.time_of_day`; `publishing_goals.episodes_per_month` — canonical schedule configuration source |
| Factory Configuration Layer | B4 | `publishing.cadence`, `.day_of_week` in ResolvedConfig — schedule config delivered to pipeline at runtime |
| Run Operating System | B7 | `scheduled` trigger type and requirements; `schedule_id` in Run Schema; conflict handling; `planned` fallback state; cadence-based maintenance and analytics runs; dashboard panels |
| Run Lifecycle Governance | B5.17 | `system:scheduler` authority; `create_run(initiated_by="scheduler")`; `operator_id=null` for scheduler; RunRecord `initiated_by` enum |
| Runtime Execution Governance | B5.16 | `new_run` context accepting scheduler; `analytics_collection` and `learning_run` contexts naming Factory OS scheduler; `deferred` clearance with `deferred_until`; slot reservation mechanism |
| Agent Operating System | B6 | Scheduler Agent with `publish.schedule` capability; time-slot assignment role |
| Agent Skills Matrix | B5.15a | "Factory OS (scheduled)" and "Learning Module scheduler" as authorized invokers |
| Platform Connector Governance | B5.10 | Native vs. factory scheduling (§12); `scheduled_publish_at` field; `scheduling_mode` field; V1 local machine clock |
| Channel Activation & Lifecycle Governance | B5.18 | Channel state as schedule lifecycle authority; `blocked`/`paused`/`archived` effects on scheduled runs; `channel_blocked` signal path |
| OIS Governance | B5.21 | Signal lifecycle for scheduler-originated run signals |
| Budget & Cost Governance | B5.11 | "channel's publishing cadence" as known quantity for cost forecasting |
