# EXECUTION PROTOCOL

**Version:** 1.0
**Status:** Operational discipline for every implementation task. Distinct from `PROJECT_PRINCIPLES.md` (what to value) — this document governs *how* work gets done, step by step.

This is not invented policy. It is a direct codification of the working pattern that was established and repeatedly validated across the architecture-stabilization work of 2026-07 (Prompt Compiler, Video DNA, Music Architecture Option B, thumbnail composition-distinctness fix, production safety gates). Every rule below reflects a real instance where following it produced a correct, evidence-backed outcome, or where deviating from it would have.

---

## 1. Purpose

To make implementation work repeatable, auditable, and resistant to confident-but-wrong assumptions — independent of which agent or person is doing the work.

## 2. Investigation Requirement (before implementing)

Before proposing or making any non-trivial change:

- Read the actual code, actual logs, actual git history, and actual data. Never infer root cause from a symptom description alone.
- When a design decision's origin or rationale is unclear, trace it (git blame/log, commit messages, related docs) rather than guessing at intent.
- State findings as evidence ("commit X on date Y changed Z, confirmed by...") not as inference ("this was probably because...").
- This mirrors `PROJECT_PRINCIPLES.md` #10 ("Never Trust Assumptions... Never guess") but applies it specifically to the implementation workflow, not just code-reading.

## 3. Approval Gate for Architectural Changes

Before implementing any change that touches a shared contract, a frozen/DNA-status module, a cross-cutting safety gate, or established behavior other systems depend on:

- Present the investigation's findings and a concrete recommendation.
- Get explicit approval before writing code.
- Routine, narrowly-scoped bug fixes with an obvious, low-risk correction do not require this gate — use judgment, but default to asking when uncertain whether something counts as "architectural."

## 4. Change-Scope Discipline

- Make the narrowest change that correctly fixes the problem.
- Extend existing patterns and idioms rather than introducing a parallel or duplicate mechanism.
- Never touch unrelated systems while fixing one thing, even if a related issue is discovered along the way.
- When an out-of-scope issue is found during investigation, **flag it explicitly and stop** — do not silently fix it, and do not silently ignore it. Record it (technical debt note, memory, or a tracked item) so it isn't lost.

## 5. Verification Standard

- Unit tests alone do not establish production readiness for anything that depends on real provider behavior, real timing, or real cost. A real, supervised execution is required before a "production-ready" claim is made.
- When both a measured (real) value and an estimated value exist, the measured value takes precedence — always. Estimates are stated as estimates, never presented as fact once a measurement exists.
- Every fix ships with regression tests that reproduce the exact real failure, not just an abstract version of it, plus reasonable edge-case coverage.

## 6. Exemption / Bypass Discipline

Any mechanism that bypasses a budget ceiling, a safety gate, a QA check, or similar control must be:

- Narrow — scoped to exactly the case it exists for, never a blanket override.
- Explicit — named, logged, and never silently inferred from an unrelated flag.
- Single-use or time-boxed where possible (e.g., a per-episode approval row, not a global config flip left in place).
- Reverted immediately after its purpose is served. A temporary override that outlives its purpose is a bug, not a convenience.

## 7. Completion Gate

A task is not complete until:

- The full test suite is clean, or every remaining failure is identified by name and confirmed pre-existing/unrelated via direct comparison against a pre-change baseline.
- Documentation and state-tracking files affected by the change are updated in the same work session — not deferred to "later."
- Any temporary environment/config overrides used during the work have been reverted.

## 8. Stop Conditions

- Do not begin unrelated feature work after completing the task that was asked for, even if an adjacent opportunity is visible.
- When a task's defined scope is done, stop and report — do not keep going "while I'm at it."
- When genuinely blocked on a decision only the requester can make (ambiguous authority, conflicting documents, missing information that can't be verified from the repo), ask — do not guess and proceed.
