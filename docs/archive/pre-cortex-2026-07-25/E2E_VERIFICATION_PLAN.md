# E2E Verification Plan

Controlled single-run proof that all 8 factory fixes work end-to-end together.
Do not deviate from this sequence. Do not run until all pre-flight checks pass.

---

## Pre-flight Checklist (manual, before running anything)

### 1. .env state
Verify these values in `.env` before the run:

```
SMOKE_TEST_MODE=false
SMOKE_TEST_MAX_NARRATION_BEATS=3    # must remain — does NOT cap beats when SMOKE_TEST_MODE=false
ENABLE_DEDICATED_SHORT_GENERATION=true
DISCOVERY_ENGINE=v2
MUAPI_DRY_RUN=false                 # must be false — real clips required for clip_availability=1.0
MUAPI_CHEAP_MODE=false              # must be false — T2 only, T3 has lower quality
EPISODE_ZERO_MAX_SCENES=0           # must be 0 — full episode, not Episode Zero
```

### 2. Queue must be empty
```bash
sqlite3 .hypogeum-assets/hypogeum.db \
  "SELECT COUNT(*) FROM run_queue WHERE status IN ('queued','claimed');"
# Expected: 0
```

### 3. No active worker
```bash
ps aux | grep run_worker | grep -v grep
# Expected: no output
```

### 4. A fresh topic with target_publish_at must be queued
The topic must be a NEW episode (not a rerun of ep_20260629075717_c7abfd).
Enqueue via the dashboard `/create` page or:
```bash
# Confirm current queued topics:
sqlite3 .hypogeum-assets/hypogeum.db \
  "SELECT topic_id, topic_text, target_publish_at, status, source FROM episode_topics WHERE status='queued';"
```
The selected topic row MUST have `target_publish_at` set to a future UTC datetime.
This is what populates `publishAt` in the YouTube API call.

### 5. Dashboard must be running
```bash
python3 run_dashboard.py --port 5001
# Running at http://127.0.0.1:5001
```

---

## The Verification Command

Run worker once, capture all output:

```bash
python3 run_worker.py --once 2>&1 | tee /tmp/e2e_verification_run.log
```

---

## Post-run Verification Queries

Run each query in order. Every assertion must pass before marking a fix verified.

### Fix 1 — Audio beat cap (33 beats, no silent scenes)
```bash
python3 -c "
import json, glob, os
# Find the most recent narration_package
eps = sorted(glob.glob('.hypogeum-assets/episodes/*/narration_package_*.json'))
path = eps[-1]
ep_id = os.path.basename(os.path.dirname(path))
with open(path) as f: np = json.load(f)
qa = np.get('audio_qa_report', {})
segs = np.get('narration_segments', [])
main_segs = [s for s in segs if s.get('source_type') == 'main_episode']
print(f'Episode: {ep_id}')
print(f'total_beats: {qa.get(\"total_beats\")}        EXPECTED: >= 30')
print(f'main_segments: {len(main_segs)}              EXPECTED: >= 30')
print(f'beats_failed: {qa.get(\"beats_failed\")}     EXPECTED: 0')
"
```
**Pass criteria:** `total_beats >= 30`, `beats_failed == 0`.

---

### Fix 2 — YouTube scheduled private (not instant public)
```bash
python3 -c "
import json, glob, os
recs = sorted(glob.glob('.hypogeum-assets/episodes/*/publishing_record_*.json'))
path = recs[-1]
ep_id = os.path.basename(os.path.dirname(path))
with open(path) as f: pr = json.load(f)
for r in pr.get('platform_results', []):
    if r.get('platform') == 'youtube' and r.get('deliverable_type') == 'main_episode':
        print(f'Episode: {ep_id}')
        print(f'YT main status: {r.get(\"status\")}')
        print(f'published_url: {r.get(\"published_url\")}')
        print()
        print('MANUAL CHECK REQUIRED:')
        print('1. Open YouTube Studio for the channel')
        print('2. Confirm the video is Scheduled (not Live/Public)')
        print('3. Confirm the scheduled go-live time matches target_publish_at from episode_topics')
        print('4. Confirm video is Private with publishAt set (not Public)')
"
```
**Pass criteria:** Video appears as Scheduled/Private in YouTube Studio with correct go-live time. Cannot be verified from DB alone — requires YouTube Studio visual check.

---

### Fix 3 — Worker not falsely orphaned
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('.hypogeum-assets/hypogeum.db')
conn.row_factory = sqlite3.Row
# Most recent run
row = conn.execute(
    \"SELECT run_id, episode_id, status, error_message FROM runs ORDER BY started_at DESC LIMIT 1\"
).fetchone()
print(f'run_id: {row[\"run_id\"]}')
print(f'status: {row[\"status\"]}            EXPECTED: completed (not complete/failed)')
print(f'error_message: {row[\"error_message\"]}  EXPECTED: None')
conn.close()
"
```
**Pass criteria:** `runs.status == 'completed'` (with 'd'), `error_message IS NULL`.
A status of `'complete'` (without 'd') or `error_message='Run orphaned — worker not active'` indicates the fix did not work.

---

### Fix 4 — Shorts: 3 real clips, no placeholders
```bash
python3 -c "
import json, glob, os
pkgs = sorted(glob.glob('.hypogeum-assets/episodes/*/final_render_package_*.json'))
path = pkgs[-1]
ep_id = os.path.basename(os.path.dirname(path))
with open(path) as f: frp = json.load(f)
shorts = frp.get('shorts_renders', [])
print(f'Episode: {ep_id}')
print(f'Total shorts_renders: {len(shorts)}        EXPECTED: 3')
for sr in shorts:
    sid = sr.get('short_id','')
    vid = sr.get('video_asset_id','')
    placeholder = '_placeholder' in sid
    print(f'  {sid}: video_asset_id={vid!r}  placeholder={placeholder}  EXPECTED: placeholder=False, video_asset_id non-empty')
"
```
**Pass criteria:** All 3 `short_id` values contain no `_placeholder`; all `video_asset_id` are non-empty strings.

---

### Fix 5 — clip_availability_rate = 1.0 (no missing clips)
```bash
python3 -c "
import json, glob, os
manifests = sorted(glob.glob('.hypogeum-assets/episodes/*/render_output_manifest_*.json'))
path = manifests[-1]
ep_id = os.path.basename(os.path.dirname(path))
with open(path) as f: m = json.load(f)
rate = m.get('clip_availability_rate', 0)
missing = m.get('missing_clips', [])
print(f'Episode: {ep_id}')
print(f'clip_availability_rate: {rate}    EXPECTED: 1.0')
print(f'missing_clips: {missing}          EXPECTED: []')
print(f'render_status: {m.get(\"render_status\")}  EXPECTED: complete (not partial)')
"
```
**Pass criteria:** `clip_availability_rate == 1.0`, `missing_clips == []`, `render_status == 'complete'`.
If rate < 1.0, the pipeline should have halted at QA (Fix 7). If it reached publishing, Fix 7 did not work.

---

### Fix 6 — Thumbnail: composition_plan + typography_plan on all 3 candidates
```bash
python3 -c "
import json, glob, os
pkgs = sorted(glob.glob('.hypogeum-assets/episodes/*/thumbnail_generation_plan_*.json'))
path = pkgs[-1]
ep_id = os.path.basename(os.path.dirname(path))
with open(path) as f: tgp = json.load(f)
cands = tgp.get('main_episode', {}).get('ranked_candidates', [])
print(f'Episode: {ep_id}')
for c in cands:
    rank = c.get('rank')
    has_comp = 'composition_plan' in c and bool(c['composition_plan'].get('text_zone'))
    has_typo = 'typography_plan' in c and bool(c['typography_plan'].get('headline'))
    has_rationale = bool(c.get('ctr_rationale'))
    focal = c.get('composition_plan', {}).get('focal_zone', 'MISSING')
    print(f'  rank={rank}: composition_plan={has_comp}  typography_plan={has_typo}  ctr_rationale={has_rationale}  focal_zone={focal!r}')
focal_zones = [c.get('composition_plan',{}).get('focal_zone') for c in cands if c.get('composition_plan')]
distinct = len(set(focal_zones)) == len(focal_zones)
print(f'focal_zones distinct: {distinct}    EXPECTED: True')
"
```
**Pass criteria:** All 3 candidates have `composition_plan` (with non-empty `text_zone`), `typography_plan` (with non-empty `headline`), non-empty `ctr_rationale`, and distinct `focal_zone` values.

---

### Fix 7 — Finance: run_id not null, total_cost_usd not null
```bash
python3 -c "
import sqlite3, json, glob, os
# Check runs table
conn = sqlite3.connect('.hypogeum-assets/hypogeum.db')
conn.row_factory = sqlite3.Row
row = conn.execute(
    \"SELECT run_id, episode_id, status, total_cost_usd FROM runs ORDER BY started_at DESC LIMIT 1\"
).fetchone()
print(f'run_id: {row[\"run_id\"]}')
print(f'total_cost_usd: {row[\"total_cost_usd\"]}    EXPECTED: non-NULL, > 0')
conn.close()

# Check finance_record
recs = sorted(glob.glob('.hypogeum-assets/episodes/*/finance_record_*.json'))
path = recs[-1]
with open(path) as f: fr = json.load(f)
print(f'finance_record.run_id: {fr.get(\"run_id\")}  EXPECTED: same as run_id above')
print(f'finance_record.cost_breakdown.total_actual_usd: {fr.get(\"cost_breakdown\",{}).get(\"total_actual_usd\")}')
"
```
**Pass criteria:** `runs.total_cost_usd` is not NULL and > 0; `finance_record.run_id` equals the run's `run_id`.

---

### Fix 8 — No NULL episode_id queue rows after the run
```bash
sqlite3 -header -column .hypogeum-assets/hypogeum.db "
SELECT COUNT(*) as null_queued_rows
FROM run_queue
WHERE episode_id IS NULL AND channel_id IS NULL AND status='queued';"
# EXPECTED: 0
```
**Pass criteria:** Count == 0. Any value > 0 means the scheduler NULL-row leak is still active.

---

## Failure Protocol

If any check fails:

| Failure | Likely cause | Action |
|---|---|---|
| `total_beats < 30` | Smoke mode or beat cap re-enabled | Check `.env` SMOKE_TEST_MODE and SMOKE_TEST_MAX_NARRATION_BEATS |
| `runs.status = 'complete'` (no 'd') | Orphan false-positive still firing | Check `/api/worker/status` during run; check `worker.pid` was written |
| `shorts_renders` has `_placeholder` | Short scenes not generated | Check `visual_package.scenes` for `source_type=short`; check `concept_brief` file exists |
| `clip_availability_rate < 1.0` | MUAPI generation failures | Check `render_output_manifest.missing_clips`; QA should have REJECTED — if it didn't, Fix 7 failed |
| `composition_plan` missing | concept_brief not found on disk | Check `thumbnail_concept_brief_{episode_id}.json` exists; check TD logs for `td_concept_brief_loaded` |
| `total_cost_usd` NULL | `write_episode_rollup()` not called or failed silently | Check pipeline log for `write_episode_rollup` call; check `costs` table for rows with this `run_id` |
| NULL queue rows > 0 | Scheduler tick still creating factory-scope scheduled_runs | Check `schedules` table for `scope='factory'` entries firing; manually cancel and investigate scheduler config |
| Video is public in YT Studio | `target_publish_at` not set on topic row, or `_get_episode_publish_at()` returned None | Check `episode_topics.target_publish_at` for the consumed row; check pipeline log for `publishing_agent_started` |

---

## Notes

- This plan uses `--once` (single iteration). The worker will tick, consume, and poll exactly once.
- The topic must NOT be a rerun of `ep_20260629075717_c7abfd` — that episode already has checkpoints, the publish guard has been triggered, and the YouTube video is already live.
- All 8 checks must pass in the SAME run. Do not declare any fix verified from a partial run.
- Check 2 (YouTube private+scheduled) requires a manual YouTube Studio visual inspection — it cannot be read back from the DB alone.
