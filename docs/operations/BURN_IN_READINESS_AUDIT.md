# Burn-In Readiness Audit and Production Reset Plan

Produced 2026-07-26. Read-only audit — no data was modified, deleted, or
reset while producing this report. Every count below was obtained via
direct `SELECT` queries against the live `.hypogeum-assets/hypogeum.db`
and direct filesystem inspection of `.hypogeum-assets/episodes/`.

---

## 0. Live process state (checked first — changes the meaning of everything below)

| Process | PID | Supervision | Since | State |
|---|---|---|---|---|
| `run_worker.py` | 17585 | launchd (`com.hypogeum.worker`, LaunchAgent, this repo's working directory) | 2026-07-25 19:40:02 (~4h24m at audit time) | Running |
| `run_dashboard.py` | 80291 | Plain background process, not launchd | 2026-07-20 23:45:24 | Running |
| `worker_control.worker_status` (DB) | — | — | last updated 2026-07-25 20:04:55 | `idle`, `active_queue_id=''` |

**The worker is live and currently idle** — it is not mid-run. This means
`run_queue`/`runs`/`stage_executions` rows found in a non-terminal status
below are **not** protected by "a live worker is using them" — they are
genuinely stale, left over from a worker instance that no longer holds
them (confirmed independently: 0 rows are `'claimed'`/`'running'` at any
level right now, see below).

**Unrelated process found, flagged, not touched**: `launchctl list` also
shows `com.hypogeum.pipeline`, a LaunchAgent running `/bin/bash
/Users/gawadeditor/faceless-channel/run_auto.sh` — a different project
in a different directory (`~/faceless-channel`), coincidentally sharing
the `com.hypogeum.*` label prefix. It has no relationship to this
repository or database and is out of scope for this audit and reset.

---

## 1. `run_queue`

| Status | Count |
|---|---|
| `cancelled` | 22 |
| `completed` | 4 |
| `failed` | 21 |
| `queued` | 0 |
| `claimed` | 0 |

By `source`: `NULL` 23, `rerun` 9, `manual` 8, `schedule` 5, `e2e_lite` 2.
No `auto_retry` rows exist yet — the auto-retry feature (`b08f8f7`) has
not fired in production.

- **Historical or active?** All 47 rows are historical — every row is
  already in a terminal state (`cancelled`/`completed`/`failed`). Nothing
  is `queued` or `claimed`, so there is no live/pending work in this
  table right now.
- **Disposition**: **Archive or clear.** Terminal rows carry no
  operational meaning for a fresh burn-in and mixing 47 historical test/
  validation rows into the queue history makes early burn-in dashboard
  views noisy. Nothing here needs to be *preserved* for correctness —
  `runs`/`stage_executions`/`publishing_outcomes` are the durable record
  of what actually happened, not `run_queue`.

## 2. `runs`

| Status | Count |
|---|---|
| `completed` | 5 |
| `complete` | 1 *(inconsistent value — see note)* |
| `failed` | 40 |
| `running` | 0 |

Note: one row uses `'complete'` instead of `'completed'` — a naming
inconsistency from an older code path, not a stuck run. No row is
currently `'running'` — consistent with the worker being idle.

- **Historical or active?** 100% historical. Zero active runs.
- **Disposition**: **Preserve as historical record, or archive — do not
  silently delete.** This table (together with `stage_executions` and
  `publishing_outcomes`) is the actual attempt/outcome history of the
  system. If the goal is a *clean dashboard view* for burn-in rather than
  literal data loss, archiving (e.g. to a dated export, or a
  `runs_pre_burnin_archive` table) preserves the record while giving
  burn-in a visually clean `/runs` page. Straight deletion is also
  acceptable if the user does not need this specific historical detail
  (the 40 failures are almost entirely superseded validation/test runs —
  see Section 11), but that is a judgment call, not a technical
  requirement.

## 3. `stage_executions`

| Status | Count |
|---|---|
| `completed` | 400 |
| `failed` | 22 |
| `running` | **6** |

**The 6 `'running'` rows are genuinely orphaned** — confirmed by cross-
reference: every one of their parent `runs` rows is already `failed`
(none is `running`), and no worker currently holds them. They predate
this session's H1 orphan-reconciliation fix reaching them (H1 only
reconciles `stage_executions` tied to a `runs` row that is orphaned *at
the moment* `reconcile_orphaned_runs()` runs — these 6 became stuck
before that mechanism existed, or during an incident window it didn't
cover) — episode_ids `ep_b14dab37`, `ep_20260629063505_3ddb0c`,
`ep_9cf89330`, `ep_reg_vi_colosseum` (all stage `visual_package_director`,
dated 2026-06-29 to 2026-07-05), `ep_7300f729` (`thumbnail_director`,
2026-07-18), `ep_marval3_c6c62dc1` (`research_director`, 2026-07-25 — a
MAR-validation artifact from this session).

- **Historical or active?** Historical/stale — not active. These 6 rows
  are the clearest concrete "interference risk" in the entire audit: a
  dashboard or any future logic that queries "what's currently running"
  by table status alone (rather than cross-referencing the worker) would
  misreport these as in-progress forever.
- **Disposition**: **Reset the 6 stuck rows to `'failed'`** (mirroring
  exactly what H1's reconciliation already does for rows it does catch —
  same `ORPHAN_ERROR`-style terminal transition, applied retroactively).
  The other 422 rows (400 completed + 22 failed) are historical —
  preserve or archive alongside `runs`.

## 4. Checkpoints (disk — `.hypogeum-assets/episodes/`)

- **83 episode directories total**, **34 GB** on disk.
- **46 directories use the `ep_...` naming convention**; of those, the
  clear majority are identifiable test/validation artifacts by name alone:
  `ep_test*`, `ep_smoke_01`, `ep_cli_*`, `ep_marval*` (×5), `ep_review*`,
  `ep_cancel_test`, `ep_normal`, `ep_xyz`, `ep_val_01`, `ep_000`/`001`/
  `002`/`003`, `ep_20260627*`–`ep_20260629*` (7 timestamp-style dirs, all
  linked to the 9 stuck `episodes` table rows below).
- **37 directories are bare UUIDs** (no `ep_` prefix) — these come from
  the `f"ep_{queue_id[:8]}"` fallback episode_id pattern in
  `queue.py::process_next()` (used when no topic-derived episode_id was
  available) or from `run_id`-keyed paths in older code. Not
  independently classifiable as test vs. real from the name alone without
  opening each one.
- The 6 stuck `stage_executions` episodes (Section 3) all have real,
  present checkpoint directories on disk — consistent with genuinely
  interrupted, never-finished runs.
- A handful of real, successfully-published episodes also have
  directories here (cross-referenced in Section 11) — e.g.
  `ep_4e35674f`, `ep_reg_vi_colosseum`, `ep_20260629075717_c7abfd`.

- **Historical or active?** All historical — no episode is currently
  mid-production (worker idle, 0 running runs).
- **Disposition**: **Preserve by default; do not bulk-delete.** These
  directories are real production artifacts (video, audio, thumbnails)
  representing 34 GB of generated media, including at least a few
  genuinely-published episodes. A "clean burn-in" does not require
  deleting prior episode assets — it requires the *queue/run/topic
  state* to be clean so the next real episode doesn't collide with or
  get confused with old state. Recommend: leave disk assets untouched in
  this pass; disk cleanup (if ever wanted) is a separate, lower-urgency
  decision the user should make explicitly given the size (34 GB) and the
  presence of real published content mixed in with test artifacts.

## 5. `episode_topics`

| Status | Count |
|---|---|
| `cancelled` | 21 |
| `claimed` | **6** |
| `consumed` | 4 |
| `queued` | 6 |
| `skipped` | 1 |

By `source`: `manual` 15, `rerun` 22, `e2e_verification` 1.

**The 6 `'claimed'` rows are stuck** — claimed between 2026-06-27 and
2026-07-25, and (per Section 0) no worker currently holds any claim.
`claim_next_topic()` only selects `'queued'` rows, so these can never be
picked up again automatically, but they sit permanently invisible in this
state — not reconciled by H1 (H1 only touches `run_queue`/
`stage_executions`/`runs`, never `episode_topics`). Two of the six are
tied to episode_ids that already have a stuck `stage_executions` row
(`ep_20260629063505_3ddb0c`, `ep_marval3_c6c62dc1`), confirming they're
the same incidents, not independent issues. The other four
(`The Antikythera Mechanism`, `The Hidden City Beneath Rome's Colosseum
Floor` ×2 distinct claims, `The Roman Colosseum Arena Floor`,
`[MARVAL2_...] The Rosetta Stone`) are older validation-session leftovers.

**The 6 `'queued'` rows are a mix**:
- Genuine-looking topic ideas with no test markers: *The Terracotta
  Army*, *The Sunken City of Thonis-Heracleion*, *The Baghdad Battery*,
  *The Voynich Manuscript*.
- Explicit validation artifacts: *"MAR Validation Test Episode — H1
  crash-recovery probe"* and *"[E2EVAL_29bf23d1] The Nebra Sky Disc..."*
  (an E2E-validation-tagged title, priority `-2000000` — an artificially
  extreme urgency value from a validation script, not a real priority).

- **Historical or active?** The `cancelled`/`consumed`/`skipped` rows
  (26 total) are historical. The `claimed` rows (6) are stale/orphaned
  active-looking state that is not actually active. The `queued` rows (6)
  are the one genuinely mixed case: some are real backlog, some are test
  artifacts.
- **Disposition**:
  - `claimed` (6): **Reset to `cancelled`** (or a topic-appropriate
    terminal state) — they cannot be legitimately claimed by anything
    right now and must not silently linger.
  - `queued`, test-tagged (2 — the MAR/E2EVAL rows): **Cancel.** Not real
    content; the extreme priority value on one of them (`-2000000`) would
    also unfairly dominate real queue ordering if left in place.
  - `queued`, real-looking (4 — Terracotta Army, Thonis-Heracleion,
    Baghdad Battery, Voynich Manuscript): **Preserve as real backlog**,
    unless the user wants burn-in to start from a fully empty backlog
    too — flagged as a decision point in the Reset Plan below.
  - `cancelled`/`consumed`/`skipped` (26): **Preserve or archive** as
    historical topic-lifecycle record; no interference risk either way
    since they're already terminal and never re-selected.

## 6. Scheduled episodes (`scheduled_runs`)

| Status | Count |
|---|---|
| `cancelled` | 3 |
| `triggered` | 11 |
| `pending` | 0 |

- **Historical or active?** All historical/terminal. No pending scheduled
  run exists right now.
- **Disposition**: **Preserve or archive.** Nothing here can interfere
  with a fresh burn-in start — there is no pending row waiting to fire.

## 7. Scheduler state (`schedules` table — distinct from `scheduled_runs`)

Two **active** schedule definitions exist:

| schedule_id | scope | channel | cadence | next_run_at | last_run_at |
|---|---|---|---|---|---|
| `f11b158d...` | `factory` | (all) | daily | **2026-07-26T08:23:56** | 2026-07-25T08:23:56 |
| `126c7202...` | `channel` | `hypogeum` | daily | **2026-07-26T15:14:48** | 2026-07-25T15:14:48 |

- **Historical or active?** **Active, live, operationally significant.**
  Both schedules are real, currently-enabled cadence definitions with
  `next_run_at` timestamps landing **today**. If burn-in starts today
  without addressing this, the scheduler will fire both on its own
  cadence during the burn-in window — which may be exactly what "burn-in"
  is supposed to exercise (unattended, scheduled production), or may be
  an uncontrolled variable if the user wants to trigger the first burn-in
  run manually/deliberately instead.
- **Disposition**: **Preserve as-is if burn-in is meant to validate
  scheduled/cadence-driven autonomous operation** (the more realistic
  test). **Reset `next_run_at` to a deliberately-chosen future time**
  only if the user wants explicit manual control over exactly when the
  first burn-in episode starts. This is a decision point, not a default
  "reset everything" action — flagged explicitly in the Reset Plan.

## 8. Worker claims (`worker_control`)

| key | value |
|---|---|
| `worker_id` | `770fbea0-0a9c-4e30-91be-a23155643e35` |
| `worker_status` | `idle` |
| `active_queue_id` | `` (empty) |

- **Historical or active?** Active — this is the live worker's own
  current-state row, consistent with the running process (Section 0).
- **Disposition**: **Preserve.** This is correct, current, non-stale
  state — it already reflects "idle, no active claim," exactly what a
  clean pre-burn-in state should look like. No action needed.

## 9. Locks

No OS-level lock files exist anywhere under `.hypogeum-assets`
(`find ... -iname "*.lock"` returned nothing). This system does not use
filesystem locks — all coordination is via `run_queue.status='claimed'` +
`worker_id`, an atomic conditional `UPDATE` (`claim_next_queue_item()`),
already covered in Section 1.

- **Disposition**: **N/A — nothing to reset.** The absence of a
  filesystem lock is itself a confirmation that there is no separate
  lock-state category to audit beyond the DB-row-based claiming already
  covered above.

## 10. Orphan state

Directly cross-referenced rather than executed (running
`reconcile_orphaned_runs()` would *write*, which this audit intentionally
avoided):

- `runs.status='running'` with no live worker: **0** (none currently).
- `run_queue.status='claimed'` with no live worker: **0**.
- `stage_executions.status='running'` with no owning active run: **6**
  (Section 3).
- `episode_topics.status='claimed'` with no live worker: **6** (Section
  5) — the one category H1's existing reconciliation does **not** cover.

- **Disposition**: The 6 `stage_executions` rows should be reset via the
  same mechanism H1 already uses (terminal `'failed'` transition). The 6
  `episode_topics` rows need a reset that doesn't currently have an
  automated path (H1 never touches this table) — this audit surfaces that
  as a manual/scripted cleanup step for the Reset Plan, not a bug to fix
  in this task.

## 11. Retry state

- `run_queue.source = 'auto_retry'`: **0 rows.** The feature (`b08f8f7`)
  has not fired in production yet — nothing to reset.
- `run_queue.retry_count > 0`: **0 rows** (consistent with the above).
- No `next_attempt_at` values are set on any row.

- **Historical or active?** N/A — no retry state exists yet.
- **Disposition**: **Nothing to do.** Clean by construction.

## 12. Pending publishing state

`publishing_outcomes`: **11 rows, all `publishing_status='success'`.**
Zero rows in `pending`/`unknown`/`failed`. Breakdown: YouTube 4, TikTok 3,
Facebook 2, Instagram 2.

**Notable data-quality finding, unrelated to burn-in readiness per se but
worth surfacing**: several rows have non-standard `episode_id` values —
raw titles (`"The Enigma of the Ubaid Lizard People"`,
`"The Real Atlantis Was Found in Greece"`) or platform post identifiers
(`"Facebook Reel 948465328243609"`, `"Instagram Reel
18105187058055594"`) instead of a real `ep_...` identifier. These look
like an early manual backfill of pre-existing published videos, done
before `episode_id` was a consistently-enforced identifier convention.
They do not reference any live `episode_topics`/`episodes`/`runs` row and
cannot collide with anything in a fresh burn-in (no code path will ever
generate a matching real `episode_id` for these values) — flagged as
historical-data-quality noise, not an operational risk.

Also notable: the `episodes` table has only **9 rows**, all
`status='in_progress'` (never finalized to `complete`/`published`),
dated 2026-06-27 to 2026-06-29 — including `ep_20260629075717_c7abfd`,
which **does** have a real corresponding `publishing_outcomes` success
row. This is a genuine inconsistency (the episode actually published, but
its own `episodes.status` was never updated) — historical, not a burn-in
blocker, but worth knowing about.

- **Historical or active?** Entirely historical. Nothing pending.
- **Disposition**: **Preserve — this is exactly the "historical
  production and analytics data" the user explicitly wants kept.** No
  reset action applies here at all.

## 13. Active runtime state — summary

- Live processes: worker (launchd, idle) + dashboard (plain process) —
  both healthy, both should simply be **left running** (or the worker
  briefly stopped/restarted only if the Reset Plan below requires it to
  avoid racing a manual DB cleanup — see Reset Plan Step 1).
- `channel_operations_profiles.hypogeum`: `production_enabled=1`,
  `publishing_enabled=1`, `discovery_enabled=1`, but
  **`unattended_operation_enabled=0` (False)**.
- CALOG: `hypogeum` channel state = `active`, `is_channel_runnable()` =
  `True`.
- OIS: `is_factory_blocked()` = `False`, `is_channel_blocked('hypogeum')`
  = `False`, no open blocking signals (29 open signals exist, all
  `INFO`/`WARNING` severity, `blocking=0` — non-blocking analytics-
  ingestion-quality notices, not operational blocks).
- `secret_profiles`: exactly one `active` row per provider (elevenlabs,
  facebook, instagram, muapi, openai, openrouter, perplexity, runway,
  seedance_official, tiktok, veo, veo_official, youtube), plus one
  `revoked` youtube row (the pre-fix token from this session's OAuth
  work, correctly superseded, not currently used).

- **This is the single most important finding in the entire audit**:
  **`unattended_operation_enabled=0` currently blocks the entire premise
  of a burn-in.** Nothing else in this audit is a hard blocker — this
  one is. Every other item is queue/history hygiene; this flag is a
  genuine precondition that must change before an unattended run can
  happen at all.
- **Disposition**: **Flip `unattended_operation_enabled` to `True`** via
  the canonical `update_channel_operations_profile()` API (never a raw
  SQL write — matches this session's own established discipline for this
  exact flag, from the MAR validation work), with before/after evidence
  captured, as an explicit, deliberate part of the Reset Plan — not
  silently bundled into "clearing stale rows."

---

## Production Reset Plan (not executed — for review)

### Goal restated

Start burn-in from a clean **operational** state — no stale claims, no
orphaned in-progress rows, no leftover test/validation content
masquerading as real backlog — while preserving every piece of real
historical production and analytics data (`runs`, `stage_executions`,
`publishing_outcomes`, disk checkpoints, and any genuine topic backlog).

### What gets reset (operational state only)

| Item | Action | Why |
|---|---|---|
| 6 stuck `stage_executions` rows (Section 3) | Transition to `'failed'` with an `ORPHAN_ERROR`-style message (same shape H1 already uses) | Genuinely orphaned; no live owner; would otherwise misreport as in-progress forever |
| 6 stuck `episode_topics` rows, `status='claimed'` (Section 5) | Transition to `'cancelled'` | No live worker holds them; cannot be legitimately claimed again; H1 does not cover this table today |
| 2 test-tagged `episode_topics` rows still `'queued'` ("MAR Validation Test Episode", "[E2EVAL_...] Nebra Sky Disc") | Transition to `'cancelled'` | Explicit validation artifacts, not real content; one carries an artificial `-2000000` priority that would distort real queue ordering |
| `channel_operations_profiles.hypogeum.unattended_operation_enabled` | `False` -> `True`, via `update_channel_operations_profile()` (canonical API, not raw SQL), before/after evidence captured | Hard precondition for any unattended run at all |

### What gets archived, not deleted (preserve the record, clean the view)

| Item | Action | Why |
|---|---|---|
| `run_queue`: 47 terminal rows (Section 1) | Export/archive, then clear from the live table (or leave in place if a clean dashboard view is not actually required — this table is not the durable record) | Historical test/validation queue activity; not needed for correctness, only for a tidy burn-in dashboard |
| `runs`: 46 terminal rows (Section 2) | Preserve in place, or archive to a dated export if a clean `/runs` view for burn-in is wanted | Durable attempt history — the user explicitly asked to preserve historical production data |
| `stage_executions`: 422 terminal rows (400 completed + 22 failed, post-fix) | Preserve in place or archive alongside `runs` | Same reasoning |
| `scheduled_runs`: 14 terminal rows (Section 6) | Preserve or archive | Historical, non-interfering |
| `episode_topics`: 26 terminal rows (`cancelled`/`consumed`/`skipped`) | Preserve or archive | Historical topic lifecycle, non-interfering |

### What is explicitly preserved, untouched

- All 11 `publishing_outcomes` rows (real publish history — exactly the
  "historical production and analytics data" requirement).
- All 34 GB of disk checkpoints under `.hypogeum-assets/episodes/` — no
  bulk deletion in this pass.
- `worker_control` (already correctly `idle`).
- `secret_profiles` (all credentials, active and revoked history).
- CALOG channel state (`active`), OIS state (unblocked).
- The 2 active `schedules` rows — **pending one decision, below.**
- The 4 real-looking `queued` `episode_topics` (Terracotta Army, Thonis-
  Heracleion, Baghdad Battery, Voynich Manuscript) — **pending one
  decision, below.**

### Two decisions for the user before execution (not defaults)

1. **Scheduler cadence** (Section 7): leave the 2 active schedules firing
   on their existing `next_run_at` (today), so burn-in exercises the real
   scheduled/autonomous path end-to-end — or reset `next_run_at` to a
   time the user chooses deliberately, for manual control over exactly
   when the first burn-in episode starts. Recommend the former (letting
   the scheduler fire naturally) since burn-in is explicitly about
   validating unattended operation, not manually triggering it — but this
   is the user's call.
2. **The 4 real-looking queued topics** (Section 5): keep them as
   genuine backlog for burn-in to consume — or cancel them too, if the
   user wants burn-in to start from a fully empty topic backlog and rely
   entirely on Discovery to populate it fresh. Recommend keeping them
   (they appear to be legitimate content, not test artifacts) unless the
   user specifically wants a zero-backlog start.

### Execution order (once approved)

1. Confirm the worker is idle (already true) before touching any row it
   could concurrently claim — no need to stop the launchd worker for
   this, since nothing being reset is currently claimed or running, but
   worth a final `worker_control` check immediately before executing.
2. Apply the two hard resets (6 `stage_executions`, 6+2 `episode_topics`)
   via the existing canonical helpers (`update_stage_execution_failed()`,
   a topics-layer cancel), not raw `UPDATE` statements — matching this
   session's established discipline of only writing through the codebase's
   own functions.
3. Flip `unattended_operation_enabled` via
   `update_channel_operations_profile()`, capture before/after evidence.
4. Resolve the two decision points above with the user.
5. Optionally archive (not delete) the terminal historical rows listed
   above, if a clean dashboard view is wanted for burn-in.
6. Re-run this same audit's read-only queries once more immediately
   after, to confirm the post-reset state matches exactly what this plan
   describes before declaring the system burn-in-ready.

**Nothing above has been executed.** This document is the complete audit
and proposed plan only, per instruction.
