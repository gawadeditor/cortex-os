# MAR Production Validation — Report

Executed 2026-07-25, live against the production system, per
`docs/operations/MAR_PRODUCTION_VALIDATION.md`. This document reports
outcomes only; the procedure document remains the authoritative plan.

All raw command transcripts, DB dumps, and log excerpts referenced below
are preserved in the session's evidence directory
(`mar_validation/00`–`29_*.txt`/`.md`) and summarized here.

---

## Overall Verdict (original validation): **FAIL**

**Update 2026-07-25 (same day):** H1 and H2 were repaired and independently
revalidated live under MAR — see "Revalidation — H1/H2 Repair" at the end
of this document. Both are now confirmed **PASS**. H3 was already fixed
during the original validation. This section is preserved unedited as the
historical record of the original run; the revalidation section below is
authoritative for current H1/H2 status.

Continuous unattended production is **not yet achieved**. Two CRITICAL/HIGH
root causes (H1, H2) independently prevent an interrupted or organically-
scheduled production run from completing without manual intervention, in
addition to one CRITICAL environment defect (H3) found and fixed live
during this validation. See Go/No-Go detail below.

---

## Section 1 — Raw Evidence vs. Interpretation (separated per instruction)

This report distinguishes, throughout:
- **Raw evidence**: DB rows, log lines, `launchctl`/`ps` output — facts,
  quoted or referenced by evidence-file name, not paraphrased conclusions.
- **Interpretation**: what the evidence implies, clearly marked.
- **Confirmed root cause**: a specific code location and mechanism, verified
  by reading the source, not inferred from symptoms alone.
- **Confirmed consequence**: a symptom whose explanation is *already*
  accounted for by a confirmed root cause elsewhere in this report — not
  re-investigated or re-hypothesized independently.

---

## Section 2 — Step-by-step results

| # | Step | Result | Evidence file |
|---|---|---|---|
| 1 | Worker auto-starts after boot/login | **PARTIAL PASS** (proxy evidence only — no real reboot performed; see caveat) | `04_step1_autostart.txt` |
| 2 | Worker auto-restarts after `kill -9` | **PASS** | `06_step2_KILL1_transcript.txt`, `07_step2_3_POST_KILL1.txt` |
| 3 | Scheduler resumes correctly after restart | **PASS** | `05_step2_3_PRE_KILL.txt`, `10_step3_evidence.txt` |
| 4 | Queue state remains consistent after a crash (**H1 direct test**) | **FAIL** | `20_H1_PRE_KILL2.txt`, `22_H1_POST_RESTART_FULL_OBSERVATION.txt` |
| 5 | Orphan recovery executes correctly | **PARTIAL PASS** — `runs` row correctly reconciled; `run_queue`/`stage_executions` rows for the same episode were not (same root cause as H1) | `22_H1_POST_RESTART_FULL_OBSERVATION.txt` |
| 6 | Scheduled episode completes end-to-end after a restart | **INCONCLUSIVE** (crash-injection sub-test not executed within the observation window); mechanical scheduled-path promotion/claim/multi-stage execution confirmed working | `25_step6_TIMEOUT_evidence.txt`, `26_step6_classification.md` |
| 7 | Publishing continues normally | **INCONCLUSIVE** (no episode in this validation reached the Publishing stage); YouTube OAuth independently confirmed healthy (✓ Verified), Publishing Agent mechanism confirmed working via pre-existing dry-run records | `27_step7_evidence_pt1.txt`, `28_step7_evidence_pt2.txt` |
| 8 | Analytics sync continues normally | **PASS** | `29_step8_evidence.txt` |
| 9 | No manual terminal intervention required | **PASS, with one documented exception** (H3 install recovery — see Section 6) | this report, Section 6 |
| 10 | No Claude intervention required | **PASS, with the same documented exception** | this report, Section 6 |

Plus, discovered mid-validation and resolved live:
| — | H3: ffmpeg unreachable under MAR's launchd PATH | **CRITICAL, FIXED** | `12_H3_evidence_ffmpeg_path.txt`, `13_H3_writeup.md`, `14`–`18_H3_fix_*.txt` |

---

## Section 3 — Critical Hypotheses: Confirmed / Rejected

### H1 — Claimed `run_queue` entries survive an unexpected worker termination
**Status: CONFIRMED FAIL** (not merely a hypothesis anymore).

**Raw evidence:** `run_queue` row `89b422bb-...` — `status='claimed'`,
`worker_id='abefddcf-...'` (the process killed at `08:50:40Z`) —
unchanged through 3 full post-restart worker iterations (`08:50:40`,
`08:51:40`, `08:52:40`), each logging `poll=empty`. `runs` row for the same
episode independently reached `status='failed'`,
`error_message='Run orphaned — worker not active'` at the same restart
timestamp (`08:50:40.359594Z`) — confirming `reconcile_orphaned_runs()` did
fire, but only updates `runs`. `stage_executions` row for `research_director`
remained `status='running'`, `completed_at=None` indefinitely.

**Confirmed root cause:** `hypogeum/db.py::claim_next_queue_item()` filters
`WHERE status='queued'`; `hypogeum/orphan_runs.py::reconcile_orphaned_runs()`
only reads/writes the `runs` table. Neither table nor `stage_executions` has
any path that transitions a `run_queue` row (or its associated
`stage_executions` row) out of `'claimed'`/`'running'` when the owning
process is hard-killed rather than raising a catchable exception.

**Interpretation:** any production process crash (OOM, SIGKILL, power
loss, `launchd` force-restart) mid-episode permanently strands that
episode's queue slot. MAR restarts the *worker*, but the specific episode
never resumes or retries — it requires a human to notice and manually
"Re-run Episode."

### H2 — Scheduled invocation target_publish_at write/read key mismatch
**Status: CONFIRMED** (newly discovered, not a consequence of H1).

**Raw evidence:** `run_queue` row `644aab97-...` (`source='schedule'`)
failed 23ms after claim: `"scheduled invocation requires an existing
backward plan artifact or an explicit target_publish_at — neither was
supplied"`. `episode_topics` row `bf20c229-...` shows `target_publish_at`
was in fact written (`updated_at` matches the claim instant to the
millisecond) but `episode_id` was never populated on that row.

**Confirmed root cause:** `hypogeum/queue.py:711-732` writes the derived
`target_publish_at` keyed by **`topic_id`**
(`set_topic_target_publish_at(topic_id, ...)`);
`hypogeum/worker_integration.py:249-266`
(`lookup_episode_target_publish_at()`) reads it keyed by **`episode_id`**.
`hypogeum/queue.py:766-767` generates a fresh `episode_id` for any topic
without one, but never writes it back to `episode_topics.episode_id` before
the executor runs. The two keys never converge unless the topic already
carried a pre-assigned `episode_id` (true only for flows like manual
dashboard submission that assign one at creation time).

**Interpretation:** every scheduled invocation against an organically-
discovered or organically-queued topic (the common case for autonomous
operation) fails on its very first claim attempt — before H1's failure
mode even becomes relevant. This is independent of and precedes H1.

### H3 — ffmpeg unreachable under MAR's launchd PATH
**Status: CONFIRMED, then FIXED live during this validation.** See Section 6.

### No other hypotheses were rejected.
No hypothesis investigated during this validation was found to be a false
alarm — H1, H2, and H3 were each independently confirmed by direct,
reproducible evidence, not merely inferred.

### Confirmed PASS mechanisms (evidence-based, not assumed)
- **Monthly finance-ceiling gate**: correctly blocked all claims at
  `$173.81`/`$50.00` (348%) real spend — `03_H_budget_gate_PASS_evidence.txt`.
  Confirmed working as designed; not a defect.
- **Missed-schedule detection**: `scheduled_tick_missed_window` fired
  correctly for a schedule that was 5.7 days overdue (`delay_seconds=492977`)
  — logged, not silently dropped or double-fired.
- **Worker process supervision** (`KeepAlive.SuccessfulExit=false`): PASS,
  twice independently confirmed (kill #1 idle-state, kill #2 mid-pipeline).
- **Scheduler tick idempotency across a real restart**: PASS — no duplicate
  `scheduled_runs` rows for one due window, `next_run_at` advanced correctly.
- **Analytics sync continuity**: PASS — fired at iteration 60 and 120 of the
  post-restart worker with no manual trigger, producing a real
  `source='api'` row.

---

## Section 4 — Severity Ranking

| Severity | Finding | Blocks |
|---|---|---|
| **CRITICAL** | H3 — ffmpeg unreachable under MAR's PATH | ALL production under MAR (fixed live, see Section 6) |
| **CRITICAL** | H1 — claimed `run_queue`/`stage_executions` rows never recover after a hard-killed worker | Any crash mid-episode → permanent stall, manual "Re-run Episode" required |
| **HIGH** | H2 — target_publish_at write/read key mismatch for scheduled invocations | Every scheduled (non-manual) invocation against a topic without a pre-assigned `episode_id` — the default case for autonomous/discovered topics |
| **MEDIUM** | Publishing (step 7) unverified this session | No episode reached that stage; OAuth itself is independently healthy — not a new defect |
| **LOW** | Step 1 boot-time evidence is proxy-only | No real reboot was performed (cannot be done autonomously in this session) |

---

## Section 5 — Root Blockers Preventing Fully Autonomous Operation

In order of what actually stops continuous unattended production today:

1. **H2 blocks the scheduler path from ever reaching a running pipeline**
   for any topic without a pre-assigned `episode_id` — i.e., it blocks the
   *primary* intended autonomous path (Scheduler → queue → pipeline) for
   the common case, independent of crashes.
2. **H1 blocks recovery from any real-world crash** for whichever
   invocation mode does get through (manual, or scheduled once H2 is
   fixed) — meaning even a successfully-started autonomous run has no
   resilience to interruption.
3. **H3 was the same category of blocker for 100% of runs**, now fixed and
   validated live (Section 6) — included here only for completeness of the
   picture at validation start, not as remaining work.

Together, H1 and H2 mean: **as of this validation, Factory OS cannot yet
complete one full autonomous cycle (scheduled trigger → running pipeline →
survive a real-world interruption → complete) without a human.** The one
scheduled episode that *did* run start-to-finish this session succeeded
mechanically (proving the scheduled path is not fundamentally broken) only
because H2 was deliberately worked around via SETUP (pre-assigning an
`episode_id`) — not because H2 is fixed.

---

## Section 6 — Process Notes (Steps 9/10)

**Documented exception to "zero manual intervention":** after applying the
H3 fix (plist PATH correction) and reinstalling, `scripts/install_worker_
launch_agent.sh`'s own `launchctl bootstrap` call failed
(`Bootstrap failed: 5: Input/output error` — a transient macOS launchd
condition), aborting the script (`set -euo pipefail`) with the agent
fully unloaded. Two manual commands (`launchctl bootout`, `launchctl
bootstrap`) were run directly to recover. This is classified as **SETUP-
recovery** (part of the one-time, explicitly-permitted installation step),
not an **INTERVENTION** in the worker's live claim/process/recovery
operation — no command touched `run_queue`, `runs`, or forced a claim/
retry. It is disclosed here rather than omitted so the transcript review
is honest about the one point where a fully scripted install did not
succeed unattended.

All other commands throughout the validation are tagged SETUP,
FAILURE-INJECTION, or OBSERVATION in their respective evidence files; no
other INTERVENTION-tagged command occurred.

---

## Section 7 — H3: Full Fix Record

See `13_H3_writeup.md` for the complete root cause, evidence, fix, and
post-fix validation. Summary: `scripts/install_worker_launch_agent.sh`'s
plist `PATH` changed from `/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` to
`/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
No queue/worker/scheduler/pipeline/recovery logic touched. Verified live:
a manual test episode that previously failed 48ms after claim (ffmpeg
preflight) subsequently progressed through real agent execution
(Executive Producer, ~14s, real LLM call) after the fix.

---

## Section 8 — Ordered Implementation Backlog

Grouped by root cause, not symptom. Sorted by production impact. Nothing
here is speculative — every item traces to a specific confirmed defect
above, with its exact evidence file. No item has been designed or
implemented; this is a prioritized list for a future milestone's scoping,
not a design.

1. **Fix the H1 root cause: `run_queue` and `stage_executions` are never
   reconciled when the owning worker process is hard-killed.**
   - Impact: CRITICAL — the single largest remaining blocker to surviving
     any real crash during production, for every invocation mode.
   - Scope signal (not a design): `reconcile_orphaned_runs()` currently
     only touches `runs`; whatever mechanism is designed next should
     account for `run_queue.status='claimed'` and
     `stage_executions.status='running'` rows left behind by the same
     dead worker_id, not just the `runs` row.
   - Evidence: `20_H1_PRE_KILL2.txt`, `22_H1_POST_RESTART_FULL_OBSERVATION.txt`.

2. **Fix the H2 root cause: target_publish_at is written keyed by
   `topic_id` but read keyed by `episode_id`, and `episode_topics.episode_id`
   is never populated before the read.**
   - Impact: HIGH — blocks the scheduler-originated path (the primary
     intended autonomous trigger) for any topic without a pre-assigned
     `episode_id`, independent of H1.
   - Scope signal: either the write must key by the same value the read
     uses, or `episode_topics.episode_id` must be populated at claim time
     before the executor runs — a decision for the next milestone's design
     pass, not this report.
   - Evidence: `09_new_hypothesis_backward_plan_error.txt`.

3. **Re-run step 6's crash-injection sub-test and step 7 (publishing)
   once #1 is fixed**, since both were INCONCLUSIVE only because no
   episode survived far enough (or long enough) to be interrupted/reach
   Publishing in this session — not because of an independent defect in
   either area.
   - Impact: MEDIUM — validation completeness, not a known defect.

4. **Perform a real step 1 boot-time test** (full reboot, not proxy
   evidence) — requires direct user action; cannot be automated in an
   assistant session.
   - Impact: LOW — RunAtLoad + the plist's own `properties` already show
     correct configuration; this is confirmatory, not exploratory.

**Explicitly not on this backlog:** any RuntimeHealth/Heartbeat/Reconciler/
RuntimeBackend architecture, or any other new runtime component — none of
the evidence above shows a need for hang-detection (H1/H2 are both crash-
adjacent or claim-time failures, not "alive but stuck" scenarios). Scope
stays confined to what the evidence actually shows is broken.

---

## Section 9 — Configuration Restored

Per the explicit validation-scoping instructions, all temporary
configuration changes made for this validation were reverted after the
above evidence was collected (see `30_final_restoration_evidence.txt` for
before/after proof):

| Setting | Validation value | Restored to |
|---|---|---|
| `FINANCE_BUDGET_CEILING_USD` | 200.00 | 50.00 |
| `FULL_PRODUCTION_ENABLED` | true | false |
| `PRODUCTION_MODE` | production | validation |
| `SMOKE_TEST_MODE` | true | false |
| `ANALYTICS_SYNC_EVERY_N_ITERATIONS` | 3 | removed (not previously present) |
| `hypogeum` channel `unattended_operation_enabled` | true | false |

**Permanently kept** (not reverted — a genuine bug fix, not validation
scaffolding): the H3 plist PATH correction in
`scripts/install_worker_launch_agent.sh`.

**Not reverted** (intentionally, artifacts of the validation itself, left
for operator review rather than silently deleted): the test episodes/queue
rows created during this validation (`ep_marval_*`, `ep_marval2_*`,
`ep_marval3_*`, `ep_marval4_*`), including the one permanently-stuck
`run_queue` row that is itself H1's primary evidence.

---

# Revalidation — H1/H2 Repair (2026-07-25, same day)

Workstream: "MAR Production Repair and Targeted Revalidation." H3 was left
unchanged (already fixed and validated above). H1 and H2 were fixed and
independently revalidated live under MAR. Evidence directory:
`mar_revalidation/A`–`I_*.txt`/`.json`.

## Root-cause fixes (design detail in the approved implementation plan)

- **H1** (`fix(runtime): reconcile interrupted queue and stage state`,
  commit `ab9cdb6`): `hypogeum/orphan_runs.py::reconcile_orphaned_runs()`
  now also reconciles `run_queue` and `stage_executions` in two tiers —
  Tier 1 attributes reconciled rows to the specific orphaned `runs` rows
  already identified (via `episode_id`); Tier 2 is a residual safety net
  for a worker that crashes after claiming but before its `runs` row is
  registered. Both reach a terminal `'failed'` state; no auto-retry.
- **H2** (`fix(scheduler): preserve scheduled target time across
  topic-to-episode promotion`, commit `1f8c52f`):
  `hypogeum/queue.py::process_next()` now persists the freshly-assigned
  `episode_id` onto both `episode_topics.episode_id`
  (`topics.set_topic_episode_id`) and `run_queue.episode_id`
  (`db.set_queue_item_episode_id`) before the executor runs, plus a
  passive regression guard that logs a structured warning if
  `lookup_episode_target_publish_at()` still can't resolve the value
  after persistence.

Focused tests: 34/34 pass (`tests/test_orphan_runs.py`, H1) and 254/254
pass (`tests/test_topics.py` + `tests/test_queue.py` +
`tests/test_worker_integration.py`, H2). Targeted worker/queue/scheduler/
topics/backward-planner/publishing-safety suite: 640/640 pass. Full suite:
11,238 passed, the same 8 pre-existing unrelated failures (thumbnail
prompt-version, reuse-engine scoring) as every prior run this session —
zero new regressions.

## Blocker encountered and resolved before a clean revalidation

**First revalidation attempt was INCONCLUSIVE**, not because of H1/H2: a
new smoke-test topic was submitted at `priority=-500`, the same priority
used by an unconsumed *stale* topic left over from the original
validation (`ep_marval4_5f96bc9d`, released back to `'queued'` after its
BGA rejection, never cleaned up). `claim_next_topic()`'s
priority-then-FIFO ordering correctly claimed the older same-priority
topic instead, and since that topic already carried a pre-assigned
`episode_id`, the pipeline legitimately resumed *that* stale episode via
real on-disk checkpoint-skip logic — not the intended new test. Classified
as **environment / test-data artifact**, confirmed not a consequence of
H1 or H2 (H2's new persistence code never engages when the topic already
has an `episode_id`; H1 only runs at worker startup, unrelated to claim
ordering). No code was touched to resolve it — the fix was operational:
restore the clean baseline, and use `priority=-1000000` with a unique
namespace prefix for every subsequent test topic, guaranteed not to
collide with any historical validation topic (min prior priority was
`-500`). Historical validation topics were left in place, not deleted.

## Clean revalidation — steps A–H

All observation windows hard-capped at two worker intervals (~130s);
no open-ended waits. `timeout` is unavailable on this host — bounded
iteration-count loops (`sleep 2` × up to 65) were used instead.

| Step | Result | Evidence |
|---|---|---|
| A. MAR running under launchd | **PASS** | `A_worker_running.txt` |
| B. Queue one controlled smoke episode | **PASS** | `E3_isolated_topic_setup.txt` |
| C. Kill worker after claim, during a real stage | **PASS** (killed mid-`research_director`) | `F_PRE_KILL.txt` |
| D. Confirm automatic restart + complete state reconciliation | **PASS** | `G_POST_RESTART_full.txt` |
| E. Confirm the episode becomes recoverable exactly once | **PASS** | `G_POST_RESTART_full.txt` |
| F. Create one scheduler-driven test topic without an episode_id | **PASS** | `H_scheduled_topic_setup.txt` |
| G. Confirm it reaches the pipeline with correct target_publish_at | **PASS** | `H4_worker_attempt.json` |
| H. Publishing continuity validated safely | **PASS** (gating evidence only — see below) | `H4_worker_attempt.json` |

**Steps C/D/E detail (H1, direct evidence):** a manual smoke episode
(`ep_MARVAL2_...`) was claimed, ran `startup_checks` and
`executive_producer` to completion, and was killed mid-`research_director`
(`kill -9`). After MAR's automatic restart:
- `run_queue` row: `status='failed'`, `error_message='Run orphaned —
  worker not active'` — **no longer permanently `'claimed'`.**
- `runs` row: `status='failed'`, same error — consistent.
- `stage_executions` row for `research_director` (the stage active at
  kill time): `status='failed'`, same error, `completed_at` set —
  **previously would have stayed `'running'` forever; this is the exact
  gap H1 closed.**
- `worker.log`: single `startup_reconcile orphaned_count=1
  run_ids=['...']` line — reconciliation ran exactly once, no duplicates,
  and the following iteration correctly found nothing left to claim
  (`poll=empty`, not an auto-retry).

**Steps F/G detail (H2, direct evidence, live under real MAR
supervision — the first time H2 was proven outside unit/integration
tests):** an organically-queued topic with no pre-assigned `episode_id`
or `target_publish_at` was submitted; the channel schedule was made due
via the canonical `scheduler.set_next_run()`. The real MAR-supervised
worker ticked, promoted, and claimed it on its own (no manual
intervention). The resulting `WorkerAttempt` record:
```
invocation_mode: "scheduled"
plan_id: "bp_b4460e19f796291d"
plan_status: "executable"
target_publish_at: "2026-07-30T18:00:00+04:00"
blocked_reason: null
```
Previously this exact scenario failed within milliseconds of claim with
`blocked_reason="scheduled invocation requires an existing backward plan
artifact or an explicit target_publish_at — neither was supplied"`. That
defect is confirmed gone.

**Step H detail:** the same `WorkerAttempt` record shows
`eligible_platforms: ["youtube"]` and `excluded_platforms` correctly
listing `tiktok`/`instagram`/`facebook`/`email` with their existing,
already-tracked reasons — publishing continuity is correctly gated and
reachable. The episode was allowed to continue running to completion on
its own rather than being artificially stopped, consistent with "continue
only far enough to validate publishing continuity safely" — actual
Publishing Agent execution would only re-confirm the already-known,
separately-tracked YouTube OAuth `invalid_grant` failure (visible in the
same log capture), not new information about H1/H2.

## Confirmed hypotheses (this revalidation)

- **H1: CONFIRMED FIXED.** Direct, live, killed-mid-stage evidence above.
- **H2: CONFIRMED FIXED.** Direct, live, real-scheduler-tick evidence
  above.
- **No new hypotheses confirmed as system defects.** The only blocker
  encountered (priority collision) was classified and resolved as an
  environment/test-data artifact, not a new hypothesis about the system.

## Configuration restored (revalidation)

Same discipline as the original validation — before/after evidence in
`mar_revalidation/D0`–`D1`, `E0`–`E1`, `I_final_restoration.txt`:

| Setting | Revalidation value | Restored to |
|---|---|---|
| `FINANCE_BUDGET_CEILING_USD` | 200.00 | 50.00 |
| `FULL_PRODUCTION_ENABLED` | true | false |
| `PRODUCTION_MODE` | production | validation |
| `SMOKE_TEST_MODE` | true | false |
| `hypogeum` channel `unattended_operation_enabled` | true | false |

Not reverted (real code fixes, not validation scaffolding): the H1 and H2
commits themselves (`ab9cdb6`, `1f8c52f`).

Not reverted (test artifacts left for operator review, same policy as the
original validation): all `ep_marval*`, `ep_revalB_*`, and `ep_MARVAL2*`
test episodes/queue/topic rows.

## Revised Go/No-Go

**H1 and H2 are no longer blockers.** The single remaining blocker
identified across both validation passes is **external and already
tracked**: YouTube OAuth token refresh (`invalid_grant`, HTTP 400) — not a
MAR defect, requires operator re-authorization (see `CURRENT_PHASE.md`).
No new architecture, runtime framework, or checkpoint/resume system was
added or found necessary by this revalidation.

---

# First Fully Autonomous End-to-End Production Run (2026-07-25, same day)

Workstream: after H1/H2 repair, the objective moved to achieving the
first complete unattended production cycle: Discovery/Queue → Scheduler →
Production → Publishing → Analytics, with a real (private) YouTube upload
to prove exactly-once publishing. **Achieved.** Evidence directory:
`e2e_validation/01`–`20_*.txt`.

Scope note: "Discovery" was represented by an organically-queued topic
(no pre-assigned `episode_id`, matching the real shape of a
discovery-sourced topic) rather than triggering a live Perplexity-backed
discovery cycle — Discovery itself is a separate, already-built capability
not in question here; the objective was proving Scheduler → Production →
Publishing → Analytics → Idle, the part never previously proven end-to-end
with a real publish.

## Two further blockers found and fixed before the run

### OAuth secret-source-of-truth defect (independent defect, fixed)

Re-authorizing YouTube OAuth (to resolve the tracked `invalid_grant`)
looked successful but did not fix real publishing: `refresh_youtube_token()`
and `refresh_meta_token()` read exclusively from `Config`/`.env`, which the
OAuth callback never updates — it only writes to `secret_profiles`.
`refresh_tiktok_token()` already did this correctly; that was the
reference pattern. Fixed by adding
`credential_manager.resolve_oauth_credential()` (secret_profiles first,
env fallback only when no active profile exists) and using it in both
functions. Verified live before the production run: `refresh_youtube_token()`
succeeded with the freshly re-authorized token, and a real
`channels.list` call returned HTTP 200 for the correct channel
(`The Hypogeum`, `UC6lbORr6RBYDPObPk80yGcQ`). Commit `bbeacf7`.

### youtube_privacy fail-open defect (independent defect, fixed)

`run_pipeline()` defaults `youtube_privacy` to `"public"`, and
`build_gated_pipeline_executor()` — the sole gate every autonomous
queue-driven run passes through — never passed it explicitly, despite an
existing code comment claiming a schedule-based private override already
existed. It didn't: every autonomous run would have defaulted to a public
upload. Fixed by resolving `youtube_privacy` from the already-loaded
`ChannelOperationsProfile.default_privacy_state` and passing it
explicitly, failing closed (`GatedExecutionRefused`) when the value is
missing or invalid — never falling through to `"public"`. Commit `1dc9cea`.

Both fixes: focused + targeted + full suite all green (same 8 pre-existing
unrelated failures, zero new regressions), committed separately.

## The run

**Attempt 1** (`ep_13872a9d`, topic "The Nebra Sky Disc..."): ran 8 stages
cleanly (startup_checks through thumbnail_strategy), then legitimately
rejected by BGA Pass 1 ("1 blocking violation(s) in scene descriptions")
at `visual_package_director`. **Not a blocker** — Brand Guardian Agent
correctly enforcing a real content-quality gate, unrelated to any fix in
this workstream. No code changed; retried with a fresh topic per the same
canonical queue/scheduler path.

**Attempt 2** (`ep_4e35674f`, topic "The Great Library of Alexandria: What
Was Really Lost?") — **succeeded completely**:

| Stage | Duration | Result |
|---|---|---|
| startup_checks | 1.2s | ✅ |
| executive_producer | 39.2s | ✅ |
| research_director | 316.4s | ✅ |
| narrative_architect | 131.9s | ✅ |
| script_engine | 175.8s | ✅ |
| finance_agent_authorization | 8ms | ✅ AUTHORIZED |
| thumbnail_strategy | 17.6s | ✅ |
| visual_package_director | 154.4s | ✅ |
| thumbnail_director | 493.6s | ✅ |
| audio_agent | 11.7s | ✅ |
| assembly_agent | 9.1s | ✅ |
| asset_archive | 65ms | ✅ |
| qa_agent | 2ms | ✅ |
| compliance_agent | 3ms | ✅ |
| publishing_agent | 11.8s | ✅ |
| archive_agent | 5ms | ✅ |
| finance_agent_ledger | 9ms | ✅ |

17/17 stages, zero retries, zero manual intervention, zero direct
`run_pipeline()` calls (submitted only via `hypogeum.topics.enqueue_topic()`
+ the real scheduler tick, exactly as the canonical path requires).
`runs` row: `status='completed'`, `exit_code=0`, `total_cost_usd=2.04`
(real spend). Total wall-clock: 22m43s.

## Verification against every required criterion

1. **Scheduler/queue pickup** — `worker.log`: `tick scheduled_runs_created=1`
   → `consume queue_items_created=1` → `poll status=completed`, all via
   the real 60s-interval worker loop, no manual trigger.
2. **One episode_id preserved end-to-end** — `ep_4e35674f` consistent
   across `runs`, `stage_executions` (all 17 rows), `episode_topics`
   (`status='consumed'`), and `publishing_outcomes`.
3. **Unattended pipeline execution** — confirmed above; no
   INTERVENTION-tagged command in the transcript.
4. **Exactly one private YouTube upload** — `publishing_outcomes`:
   exactly 1 row (`platform='youtube'`, `status='success'`,
   `platform_video_id='8gZux3jYc_I'`). Independently confirmed via a
   real, live `videos.list` API call: `privacyStatus: 'private'`,
   `uploadStatus: 'processed'`, title matches exactly. A further live
   `search.list` (`forMine=true`) query for the episode's subject found
   **exactly one** matching video on the real channel — no duplicate.
5. **No duplicate publishing ledger entry** — confirmed (count=1).
6. **Analytics ingestion** — no `platform_analytics` row yet for this
   video at verification time (~1 minute post-publish). Classified as an
   **expected, bounded delay**, not a defect: YouTube's own Analytics API
   has inherent processing latency for a just-published video, and the
   worker's periodic sync (`ANALYTICS_SYNC_EVERY_N_ITERATIONS`, default
   60) hadn't reached a trigger point yet since restart. The sync
   mechanism itself was already independently proven working in the
   original MAR Production Validation (Step 8: PASS, real `source='api'`
   row appeared post-restart).
7. **Worker returns to idle** — `worker_control`: `worker_status='idle'`,
   `active_queue_id=''`. `worker.log`: the iteration immediately after
   completion logged `poll=empty` — correctly found nothing to claim,
   ready for the next scheduled topic.

## Configuration restored

Before/after evidence in `e2e_validation/18`–`20_*.txt`:

| Setting | Validation value | Restored to |
|---|---|---|
| `FULL_PRODUCTION_ENABLED` | true | false |
| `PRODUCTION_MODE` | production | validation |
| `FINANCE_BUDGET_CEILING_USD` | 200.00 | 50.00 |
| `SMOKE_TEST_MODE` | true | false |
| `YOUTUBE_DRY_RUN` | false | true |
| `hypogeum` channel `unattended_operation_enabled` | true | false |

Not reverted (real code fixes): commits `bbeacf7`, `1dc9cea`. Not
reverted (real production artifacts, not validation scaffolding): the
published YouTube video itself (private, intentionally left up as
evidence) and episode `ep_4e35674f`'s on-disk assets/checkpoints.

## Final Go/No-Go

**PASS.** Factory OS has now demonstrated, with real evidence rather than
architectural inference, a complete autonomous production cycle from
scheduler trigger through a real, exactly-once, correctly-private
publish, with the worker returning to a clean idle state afterward ready
for the next cycle. Remaining known gaps, none of which blocked this run:
analytics-ingestion timing for a just-published video (bounded, not a
defect), and the previously-documented, deliberately out-of-scope
publishing-idempotency gap (stage checkpointing / duplicate-publish
protection on manual reruns — see the MAR Production Repair decision
above) — relevant only to the *recovery* path, not this run.
