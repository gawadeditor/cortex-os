# Factory Fix Ledger

Audit of last 8 commits. Honest classification of what is code-fixed vs. verified
in a real production run. "CODE_FIXED" means the fix exists in committed code and
passes unit tests only. "NEEDS_E2E_VERIFICATION" means no post-fix production run
has been executed with the fix active.

---

## FIX 1 — Audio beat cap silently limited production runs to 3 beats

| Field | Detail |
|---|---|
| **Commit** | `5058065` |
| **Files** | `hypogeum/agents/audio_agent.py`, `tests/test_audio_agent.py` |
| **Issue** | `.env` had `SMOKE_TEST_MAX_NARRATION_BEATS=3`. Guard checked `> 0` without requiring `SMOKE_TEST_MODE=true`. All production runs capped at 3 narration beats, producing 30/33 silent scenes. |
| **Root cause** | `audio_agent.py:154`: `if config.SMOKE_TEST_MAX_NARRATION_BEATS > 0` — no `SMOKE_TEST_MODE` gate. |
| **Fix** | Changed guard to `if config.SMOKE_TEST_MODE and config.SMOKE_TEST_MAX_NARRATION_BEATS > 0`. |
| **Tests passed** | 99 (audio agent suite) + 7 targeted beat-cap tests |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | Next production run completes all 33 narration beats; narration_package.audio_qa_report.total_beats == 33; no silent scenes in timeline_qa. |
| **Proof result** | Run `eb4ebc17` was executed after this fix and did produce 33 segments — confirmed by narration_package. **But** this run also exposed further bugs (orphan false-positive, QA rejection path). Cannot count as clean verification. |

---

## FIX 2 — YouTube main video published immediately public instead of scheduled private

| Field | Detail |
|---|---|
| **Commit** | `9c739f9` |
| **Files** | `hypogeum/agents/publishing_agent.py`, `hypogeum/pipeline.py`, `hypogeum/publishing/youtube.py`, `tests/test_publishing_agent.py` |
| **Issue** | `queue.py` calls `run_pipeline()` with no `youtube_privacy` arg → default `"public"`. No `publishAt` was ever sent to the YouTube API. Video `YXtmSNSmkhE` went live immediately as public. |
| **Root cause** | `run_pipeline(youtube_privacy="public")` default; no schedule lookup; no `publishAt` in video metadata body. |
| **Fix** | `_get_episode_publish_at()` reads `target_publish_at` from `episode_topics`; publishing agent: `publish_at` set → `privacyStatus=private + publishAt`; no schedule → block upload. |
| **Tests passed** | 33 (full publishing agent suite) + 6 schedule guard tests |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | Next production run with `target_publish_at` set: YouTube API receives `status.privacyStatus="private"` + `status.publishAt=<ISO datetime>`; video appears as scheduled (not live) in YouTube Studio. |
| **Proof result** | **NOT VERIFIED.** The only real-run post-fix (`eb4ebc17`) happened before this commit. No subsequent full pipeline run has been executed. |

---

## FIX 3 — Orphan false-positive: CLI worker marked active runs as orphaned

| Field | Detail |
|---|---|
| **Commit** | `3f45613` |
| **Files** | `run_worker.py`, `tests/test_orphan_runs.py` |
| **Issue** | `run_worker.py` never wrote `worker.pid`. Dashboard `_get_worker_pid()` returned `None` for CLI workers → `worker_process_alive=False` → `reconcile_orphaned_runs()` marked active runs as failed. Run `eb4ebc17` was orphaned mid-execution then recovered via fallback, leaving `status='complete'` and orphan error message. |
| **Root cause** | PID file only written by dashboard's `/api/worker/start`; CLI path invisible to `_get_worker_pid()`. `worker_is_running_pipeline = (False AND 'running' in active_statuses) = False`. |
| **Fix** | Worker writes `_WORKER_PID_FILE` at startup; `atexit` handler deletes it on exit. |
| **Tests passed** | 26 (orphan runs suite) |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | CLI `run_worker.py --once` run: `worker.pid` exists during pipeline execution; dashboard visiting `/api/worker/status` does NOT mark run as failed; run completes with `status='completed'` (not `'complete'`). |
| **Proof result** | **NOT VERIFIED.** The run that exposed this bug (`eb4ebc17`) predates the fix. No post-fix CLI worker run has been executed. |

---

## FIX 4 — Shorts pipeline: ENABLE_DEDICATED_SHORT_GENERATION off by default; placeholder shorts counted as failures

| Field | Detail |
|---|---|
| **Commits** | `8e8ec0a` + `269c97b` |
| **Files** | `hypogeum/config.py`, `hypogeum/agents/publishing_agent.py`, `tests/test_publishing_agent.py`, `tests/test_assembly_agent.py` |
| **Issue** | (a) `ENABLE_DEDICATED_SHORT_GENERATION` defaulted to `False` → VPD never planned 9:16 short scenes → assembly padded 3 placeholder stubs → publishing failed 3×3=9 short deliverables, hit PDR threshold, issued MAJOR failure report. (b) Placeholder short_ids (`sh_000_placeholder`) were counted as `failed/video_asset_missing` instead of `skipped`. |
| **Root cause** | Config default `False`; publishing loops checked only `video_asset_id==""` but didn't distinguish placeholder stubs from real missing assets. |
| **Fix** | `config.py` default → `True`; publishing agent skips entries where `"_placeholder" in short_id and not sr_video_id` with `status=skipped/short_not_produced`. |
| **Tests passed** | 43 (publishing agent) + 13 assembly/publishing short tests |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | Next production run: VPD plans short scenes (`source_type=short` in visual_package.scenes); assembly produces 3 non-placeholder shorts_renders with real `video_asset_id`; publishing uploads 3 YouTube Shorts as `private+publishAt`. |
| **Proof result** | **NOT VERIFIED.** No post-fix production run. |

---

## FIX 5 — Queue scheduler NULL episode row leak blocking reruns

| Field | Detail |
|---|---|
| **Commit** | `e353004` |
| **Files** | `hypogeum/queue.py`, `tests/test_queue.py` |
| **Issue** | Factory-scope schedule (`f11b158d`, `channel_id=NULL`) fires every tick → `consume_scheduled_runs()` created `run_queue` rows with `channel_id=NULL, episode_id=NULL` → `poll()` returned `no_topic` and **released** (not cancelled) the item → item re-queued → blocked real reruns with higher queue priority (-10 vs 0). Happened twice during this session, required manual SQL cleanup both times. |
| **Root cause** | `consume_scheduled_runs()` promoted all `pending` scheduled_runs unconditionally, including those with no channel or episode context. |
| **Fix** | Skips promotion when `channel_id IS NULL AND episode_id IS NULL`; cancels existing NULL-context `queued`/`claimed` rows at the start of each consume call. |
| **Tests passed** | 121 (full queue suite) |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | Two consecutive `run_worker.py --once` runs: no NULL-episode queue rows created; `api/queue` shows 0 NULL-channel items after tick+consume; worker poll reaches real reruns. |
| **Proof result** | **NOT VERIFIED.** Fix applied; live DB had one NULL row which was manually cancelled before fix was committed. |

---

## FIX 6 — Finance: `runs.total_cost_usd` NULL, `finance_record.run_id` missing

| Field | Detail |
|---|---|
| **Commit** | `54dd158` |
| **Files** | `hypogeum/agents/finance_agent.py`, `hypogeum/finance.py`, `hypogeum/pipeline.py`, `tests/test_finance.py` |
| **Issue** | `runs.total_cost_usd` is NULL for all 3 runs on `ep_20260629075717_c7abfd`. `finance_record.run_id=None`. Dashboard cost KPIs show nothing. |
| **Root cause** | (a) `run_cost_ledger()` had no `run_id` parameter. (b) `write_episode_rollup()` computed cost from `costs` table but never wrote it back to `runs.total_cost_usd`. (c) `_run_cost_ledger()` wrapper in `pipeline.py` didn't pass `run_id`. |
| **Fix** | Added `run_id` param to `run_cost_ledger()` and record dict; `write_episode_rollup()` now writes episode-total to `runs.total_cost_usd`; pipeline passes `run_id`. |
| **Tests passed** | 156 (finance + finance_agent suites, excluding 1 pre-existing schema count failure) |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | Next production run: `runs.total_cost_usd` is non-NULL after `complete_run()`; `finance_record.run_id` equals the run_id from `runs` table; dashboard `/performance` shows non-zero episode cost. |
| **Proof result** | **NOT VERIFIED.** Existing rows remain NULL (fix is forward-only). Pre-existing `eb4ebc17` record: `finance_record.run_id=None` still (written before fix). |

---

## FIX 7 — Freeze frames: missing clips allowed through to publishing

| Field | Detail |
|---|---|
| **Commit** | `257abd4` |
| **Files** | `hypogeum/agents/final_render_reviewer.py`, `tests/test_final_render_reviewer.py` |
| **Issue** | Threshold was `clip_rate < 0.60` → fail. Episode `ep_20260629075717_c7abfd` had 87.9% availability (4/33 missing). `final_render_reviewer` set `production_ready=True` (CONDITIONAL). QA passed. Video `YXtmSNSmkhE` was published with 4 scenes rendered as tpad freeze-holds (379 seconds of frozen frames). |
| **Root cause** | `final_render_reviewer.py:188`: `if clip_rate < 0.60` — missing clips between 60%–99% produced only a warning, not a blocking failure. |
| **Fix** | Changed to `if clip_rate < 1.0` → blocking failure with message naming freeze frames. Any missing clip → `production_ready=False` → QA REJECTED → pipeline halts before publishing. |
| **Tests passed** | 64 (final render reviewer) + 63 (QA agent) |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | Production run where any clip fails to generate: pipeline halts at QA stage with `verdict=REJECTED`; `failures` list includes `clip_availability`; Publishing Agent is never reached; episode not uploaded. |
| **Proof result** | **NOT VERIFIED.** The published episode `YXtmSNSmkhE` predates the fix and is already live with freeze frames. |

---

## FIX 8 — Thumbnail system: old generic path passes QA; layered system not enforced

| Field | Detail |
|---|---|
| **Commit** | `7ee79f8` |
| **Files** | `hypogeum/agents/thumbnail_qa.py`, `tests/test_thumbnail_qa.py` |
| **Issue** | `composition_plan` and `typography_plan` are only attached to candidates in concept-driven mode (when `thumbnail_concept_brief_{episode_id}.json` exists). Without them, Claude returns plain candidates with no safe text zone, no structured headline, no curiosity-gap reasoning. `run_thumbnail_qa()` never checked for their presence, so old-path thumbnails passed QA. |
| **Root cause** | `thumbnail_qa.py` had no checks for `composition_plan`, `typography_plan`, `ctr_rationale`, or composition distinctness across candidates. |
| **Fix** | Added 4 new blocking checks to `run_thumbnail_qa()`: F `c_thumbnail_composition_plan` (text_zone required), G `c_thumbnail_typography_plan` (headline required), H `c_thumbnail_curiosity_gap` (ctr_rationale required), I `c_thumbnail_composition_distinctness` (3 candidates must have distinct focal_zones). Any failure → `overall_status=fail` → QA agent `c_thumbnail_qa=fail` → REJECTED verdict. |
| **Tests passed** | 128 (thumbnail QA + director + prompt writer suites) |
| **Status** | `NEEDS_E2E_VERIFICATION` |
| **Proof required** | Next production run: `thumbnail_qa_report.overall_status=pass`; `c_thumbnail_composition_plan=pass`; `c_thumbnail_typography_plan=pass`; `c_thumbnail_curiosity_gap=pass`; 3 candidates have distinct `focal_zone` values; `thumbnail_concept_brief_{episode_id}.json` exists on disk before TD runs. |
| **Proof result** | **NOT VERIFIED.** The episode `ep_20260629075717_c7abfd` thumbnail was generated with concept-driven mode (concept brief was present) and has correct plans — but this predates the QA enforcement. Whether concept-driven mode is consistently triggered on future runs is untested. |

---

## Summary

| Fix | Commit | Status | Post-fix Real Run Exists? |
|---|---|---|---|
| Audio beat cap | `5058065` | `NEEDS_E2E_VERIFICATION` | Partial — run happened but mixed with other bugs |
| YouTube scheduled publishing | `9c739f9` | `NEEDS_E2E_VERIFICATION` | No |
| Orphan worker detection | `3f45613` | `NEEDS_E2E_VERIFICATION` | No |
| Shorts pipeline | `8e8ec0a`+`269c97b` | `NEEDS_E2E_VERIFICATION` | No |
| Queue scheduler NULL rows | `e353004` | `NEEDS_E2E_VERIFICATION` | No |
| Finance cost rollup | `54dd158` | `NEEDS_E2E_VERIFICATION` | No |
| Freeze frame guard | `257abd4` | `NEEDS_E2E_VERIFICATION` | No |
| Thumbnail layered system | `7ee79f8` | `NEEDS_E2E_VERIFICATION` | No |

**All 8 fixes are CODE_FIXED and TESTED_LOCALLY with unit tests only.**
**None have been verified in a clean post-fix production run.**

The minimum E2E verification requirement is one successful full pipeline run on a new episode (not a rerun of `ep_20260629075717_c7abfd`) that:
1. Completes all 16 stages without QA rejection
2. Uploads as `private+publishAt` (not public)
3. Generates 33 narration beats
4. Generates 3 real short video clips
5. Shows `runs.total_cost_usd` populated in DB
6. All clips present (clip_availability_rate=1.0)
7. Thumbnail has composition_plan + typography_plan on all 3 candidates
8. Worker completes without orphan false-positive
