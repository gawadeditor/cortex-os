# CURRENT_PHASE.md

# Cortex Current Phase

Version: 2.0

Status: Living Document

Authority: High

---

# Purpose

This document answers one narrow question: **what phase is active right
now, and what is and isn't allowed to happen during it?**

`ROADMAP.md` defines the full phase sequence and each phase's exit
criteria — this file is a pointer into it, kept short so it stays
accurate. `CURRENT_PROGRESS.md` tracks the task-by-task backlog for the
active phase. `CURRENT_STATE.md` remains the full implementation-state
snapshot; this file does not duplicate it.

---

# Active Phase

**Phase 3 P0 complete (2026-07-27).** Since then, work has run ahead of
the strict roadmap order toward a **sellable multi-channel product** —
guided by the North Star Completion Plan's six tracks (companion doc,
2026-09-01). See `CURRENT_STATE.md` for the full picture.

Active tracks (2026-09-01), in parallel:

- **A — Channel identity.** Per-channel brand/visual identity to every
  generation surface. First increment shipped (video scene prompts).
- **B — Sustained operation → Release Candidate.** Accumulate unattended
  runs; wire monitoring signals to alerts; runbooks. DB backup scheduled
  + healthy.
- **F — Doc re-sync.** This pass.

Next (gated): C — learning loop maturity (needs B's data); then D —
Factory Capability Evolution; E — productization runs throughout.

Original Phase 3 purpose (still holds): transform Cortex from "working"
into "safe to run unattended for weeks."

---

# What Preceded This Phase

- Phase 0 (Foundation) — complete
- Phase 1 (Autonomous Production, including Multi-Platform Publishing
  code) — complete; four platforms' publishing code verified correct by
  live evidence, three blocked on external (non-code) items
- Phase 2 (Autonomous Runtime) — exit criteria met

Phase 3's Entry Evidence (`ROADMAP.md`) — confirmed via the
Operational Validation audit, not carried forward as its own phase per
`ROADMAP.md`'s entry-evidence rule: documentation closure, external
blockers identified and documented
(`docs/operations/PLATFORM_READINESS.md`), burn-in started
(`docs/operations/BURN_IN_LOG.md`), readiness audit complete
(`docs/operations/BURN_IN_READINESS_AUDIT.md`).

The audit that opened this phase explicitly confirmed: **implementation
work is complete.** What remains is hardening, not building.

---

# What This Phase Requires (P0 — mandatory before Release Candidate)

**Status: Complete (2026-07-27).** Implemented one at a time, in order,
no batching — per this phase's own Execution Discipline below:

1. Log Rotation — done
2. Recovery System — done
3. Runtime Observability — done
4. Recovery & Self-Healing — done
5. Startup & Shutdown Verification — done

Full detail, scope, and acceptance criteria per item: see
`CURRENT_PROGRESS.md`. All P0 code is new and has not yet been observed
through a real unattended burn-in cycle — that observation is still
required before Release Candidate evaluation (see `ROADMAP.md`'s Release
Candidate Criteria; P0 completion is one line item among several, not
the whole bar). The live dashboard process also needs a restart to pick
up the new `/api/observability/health` route — not done as part of this
implementation session.

P1 (Operational Profiles, **Model and Provider Decoupling**, Operational
Health Dashboard, Runbook improvements, automated queue/run janitor,
artifact-backup risk assessment, Maintenance Mode) and P2 (remaining
technical debt) follow P0 and are not individually gating for Release
Candidate evaluation, but RC is not evaluated until the full checklist
in `ROADMAP.md`'s Release Candidate Criteria is satisfied.

**Model and Provider Decoupling** (P1) deserves its own callout:
architectural preparation only (versioned model/provider registry,
capability-based routing, fallback chains, lifecycle status, decision
audit trail) so Cortex is never hard-dependent on today's models/
providers. It does **not** include autonomous discovery or benchmarking
— that is Phase 6 (Factory Capability Evolution, `ROADMAP.md`), a much
later, evidence-gated phase not being built now. See `DECISION_LOG.md`
ADR-012.

---

# Working Rules (in effect until this phase's P0 is complete)

Do **not** begin:

- Analytics (Phase 4)
- Channel Learning / Factory Learning (Phase 5)
- Dashboard evolution / UX improvements
- Any other new feature work

...unless the work directly removes a Production Hardening blocker.

This mirrors the existing burn-in discipline already in effect (no code
change without an observed production failure; no speculative/
preventive/cleanup work) — Production Hardening P0 is the one
explicitly-scoped exception, since it exists specifically to address
gaps the audit and subsequent architecture review identified, not
speculative future needs.

---

# Execution Discipline

Per the approved architecture review: each P0 capability is implemented,
tested, and documented before the next one begins. `CURRENT_PROGRESS.md`
is updated after each capability completes — not batched at the end.
"Recovery & Self-Healing" (item 4) is verification and extension of
existing mechanisms only (launchd supervision and checkpoint-resume are
not rebuilt).

---

# Exit Condition

This phase ends, and Release Candidate evaluation may begin, only when
every item in `ROADMAP.md`'s Release Candidate Criteria is true. See that
section for the full checklist and `CURRENT_PROGRESS.md` for live status.

---

# Authority

If this document conflicts with `ROADMAP.md`, `ROADMAP.md` prevails —
this file is a pointer, not an independent source of truth. If this
document conflicts with observed reality, update this document.
