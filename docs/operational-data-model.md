# Operational Data Model — v2 Design

**Status:** Design (not yet implemented)  
**Author:** Factory Governance  
**Date:** 2026-06-12  
**Supersedes:** v1 schema in `hypogeum/schema.sql`  
**Migration target:** `hypogeum/schema.sql` v2 (append-only; all new tables + ALTER ADD COLUMN)

---

## 0. Purpose

The v1 schema (8 tables: runs, episodes, assets, costs, thumbnail_candidates, titles,
kg_seeds, signal_records) was designed for minimum viable write coverage. It records
that things happened but cannot answer operational questions about _how_, _why_, _how much_,
or _how well_.

The v2 model closes every gap required for the 8 infrastructure milestones:

| Milestone | v1 gap | v2 solution |
|---|---|---|
| Asset Registry | `assets` table has no scene FK, no file_size, no status, no retry tracking | Extend `assets`; add `asset_versions` |
| Archive Recall Layer | No recall index; no cross-episode asset reuse | Add `archive_index` |
| Cost Accounting | No per-stage breakdown; no budget gate record; no retry waste | Extend `costs`; add `budget_authorizations` |
| Factory Dashboard | No stage timing; no gate pass/fail rates | Add `stage_executions`; add `gate_verdicts` |
| Analytics Ingestion | `signal_records` is per-run, not time-series; no thumbnail signal | Add `signal_windows`; extend `signal_records` |
| Learning Layer | No decision quality records; no outcome linkage | Add `learning_records` |
| Operational Database | Schema not indexed; no views for dashboard | Add indexes + views |
| Assembly Agent | No Shorts deliverable table; no render timeline record | Add `shorts`; add `render_timelines` |

---

## 1. Existing v1 Tables — Audit

### 1.1 What v1 has (keep, some extended)

| Table | Row meaning | v1 status | v2 action |
|---|---|---|---|
| `runs` | One pipeline execution | Good foundation | Extend: add `duration_ms`, `stage_count`, `stage_success_count` |
| `episodes` | One episode (may have many runs) | Good foundation | Extend: add `youtube_video_id`, `published_at`, `duration_seconds`, `short_count` |
| `assets` | One generated file | Thin | Extend heavily (see §3) |
| `costs` | One billable API call | Thin | Extend: add `stage_name`, `attempt_number`, `asset_id` |
| `thumbnail_candidates` | One TD candidate | Adequate | Extend: add `bga_verdict`, `tig_verdict`, `attempt_count`, `final_asset_id` |
| `titles` | One title candidate | Adequate | No changes |
| `kg_seeds` | One KG extraction | Adequate | No changes |
| `signal_records` | One analytics ingestion | Thin | Extend: add `ctr`, `avg_view_duration_pct`; used as summary; windows go in `signal_windows` |

### 1.2 What v1 is missing

```
stage_executions    — per-stage timing and status (critical for dashboard)
gate_verdicts       — BGA Pass 1/2, TIG, QA, Compliance verdicts (searchable)
asset_versions      — retry attempts per asset slot (tracks waste, quality drift)
budget_authorizations — Finance Agent pre-run gate records
shorts              — Shorts deliverable tracking
render_timelines    — Assembly timeline entries (scene → clip mapping)
signal_windows      — per-window YouTube analytics time series
archive_index       — cross-episode asset recall metadata
kg_entities         — extracted entities for world bible recall
kg_relationships    — extracted relationships
learning_records    — per-episode factory learning signals
```

---

## 2. v2 Entity Relationship Map

```
episodes ──< runs ──< stage_executions
                  ──< gate_verdicts
                  ──< costs
                  ──< assets ──< asset_versions
                  ──< budget_authorizations
                  ──< thumbnail_candidates
                  ──< titles
                  ──< shorts ──< assets (via episode_id + short_id)
                  ──< render_timelines
episodes ──< signal_records
         ──< signal_windows
         ──< kg_seeds ──< kg_entities
                      ──< kg_relationships
         ──< archive_index
         ──< learning_records
```

All tables have `run_id` FK to `runs`. Gate verdict tables have both `run_id` and `stage_execution_id`.

---

## 3. Table Specifications

### 3.1 `runs` (extend v1)

New columns (ADD COLUMN — null-safe, all nullable or with DEFAULT):

```sql
ALTER TABLE runs ADD COLUMN duration_ms         INTEGER;   -- completed_at - started_at in ms
ALTER TABLE runs ADD COLUMN stage_count         INTEGER;   -- number of stages attempted
ALTER TABLE runs ADD COLUMN stage_success_count INTEGER;   -- number that completed ok
ALTER TABLE runs ADD COLUMN total_cost_usd      REAL;      -- denormalized rollup at run close
ALTER TABLE runs ADD COLUMN resume_from_stage   TEXT;      -- set when run is a checkpoint resume
ALTER TABLE runs ADD COLUMN pipeline_version    TEXT;      -- git SHA or version tag at run time
```

**Answers:** How long did this run take? How many stages succeeded? What did it cost? Was this a resume?

---

### 3.2 `episodes` (extend v1)

New columns:

```sql
ALTER TABLE episodes ADD COLUMN youtube_video_id   TEXT;   -- set after publishing
ALTER TABLE episodes ADD COLUMN published_at        TEXT;   -- ISO 8601 UTC
ALTER TABLE episodes ADD COLUMN duration_seconds    INTEGER; -- final rendered video duration
ALTER TABLE episodes ADD COLUMN short_count         INTEGER DEFAULT 0; -- number of Shorts produced
ALTER TABLE episodes ADD COLUMN total_cost_usd      REAL;   -- denormalized from finance rollup
ALTER TABLE episodes ADD COLUMN run_count           INTEGER DEFAULT 1; -- how many runs attempted
```

**Answers:** What is the YouTube video ID for this episode? When was it published? How long is it? How much did it cost total?

---

### 3.3 `stage_executions` (NEW — critical for dashboard)

One row per pipeline stage per run. Written by `pipeline.py` at `pipeline_stage_started` /
`pipeline_stage_completed` / `pipeline_stage_failed` events.

```sql
CREATE TABLE IF NOT EXISTS stage_executions (
    stage_exec_id     TEXT    NOT NULL PRIMARY KEY,  -- UUID4
    run_id            TEXT    NOT NULL,
    episode_id        TEXT    NOT NULL,
    stage_name        TEXT    NOT NULL,   -- "executive_producer" | "research_director" |
                                          -- "narrative_architect" | "script_engine" |
                                          -- "visual_package_director" | "thumbnail_director" |
                                          -- "audio_agent" | "assembly_agent" |
                                          -- "bga_pass1" | "bga_pass2" | "tig" |
                                          -- "qa_agent" | "compliance_agent" |
                                          -- "publishing_agent" | "archive_agent" |
                                          -- "finance_authorization" | "finance_ledger" |
                                          -- "asset_archive"
    started_at        TEXT    NOT NULL,
    completed_at      TEXT,
    duration_ms       INTEGER,
    status            TEXT    NOT NULL DEFAULT 'running',
                                          -- "running" | "completed" | "failed" | "skipped"
    skipped           INTEGER NOT NULL DEFAULT 0,   -- 1 = loaded from checkpoint
    error_message     TEXT,
    output_schema     TEXT,               -- schema_name of output package (e.g. "script_package")
    model_slug        TEXT,               -- primary model used (if LLM stage)
    cost_usd          REAL,               -- stage-level cost (denormalized from costs WHERE stage_name)
    metadata_version  TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_stage_executions_run     ON stage_executions(run_id);
CREATE INDEX IF NOT EXISTS idx_stage_executions_episode ON stage_executions(episode_id);
CREATE INDEX IF NOT EXISTS idx_stage_executions_stage   ON stage_executions(stage_name);
```

**Answers:**
- Which stage is the pipeline bottleneck? (`AVG(duration_ms) GROUP BY stage_name`)
- Which stage fails most often? (`COUNT(*) WHERE status='failed' GROUP BY stage_name`)
- What is the per-stage cost? (`SUM(cost_usd) GROUP BY stage_name`)
- Which stages were skipped (checkpoint resumes)?

---

### 3.4 `gate_verdicts` (NEW — critical for brand quality analytics)

One row per gate evaluation. Covers BGA Pass 1, BGA Pass 2, TIG, QA Agent, Compliance Agent.

```sql
CREATE TABLE IF NOT EXISTS gate_verdicts (
    verdict_id        TEXT    NOT NULL PRIMARY KEY,  -- UUID4
    run_id            TEXT    NOT NULL,
    episode_id        TEXT    NOT NULL,
    stage_exec_id     TEXT,                          -- FK to stage_executions (nullable for legacy)
    gate_name         TEXT    NOT NULL,              -- "bga_pass1" | "bga_pass2" | "tig" |
                                                     -- "qa_agent" | "compliance_agent" | "finance_authorization"
    verdict           TEXT    NOT NULL,              -- "APPROVED" | "APPROVED_WITH_FLAGS" |
                                                     -- "WARNING" | "REJECTED" | "BLOCKED" |
                                                     -- "APPROVED" | "error"
    evaluated_at      TEXT    NOT NULL,
    asset_id          TEXT,               -- if verdict is per-asset (TIG, BGA on thumbnail)
    rule_ids_flagged  TEXT,               -- JSON array of rule IDs flagged (e.g. ["E-C05", "E-TH01"])
    flag_count        INTEGER NOT NULL DEFAULT 0,
    blocking_count    INTEGER NOT NULL DEFAULT 0,  -- rules that would block
    warning_count     INTEGER NOT NULL DEFAULT 0,
    model_slug        TEXT,               -- model that produced the verdict
    full_report       TEXT,               -- JSON blob of the complete report
    metadata_version  TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_gate_verdicts_run      ON gate_verdicts(run_id);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_episode  ON gate_verdicts(episode_id);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_gate     ON gate_verdicts(gate_name);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_verdict  ON gate_verdicts(verdict);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_asset    ON gate_verdicts(asset_id);
```

**Answers:**
- What is the BGA pass rate across episodes?
- Which brand rules are flagged most often?
- How often does TIG block a thumbnail?
- Which episodes required BGA re-runs?
- Is brand compliance improving over time?

---

### 3.5 `assets` (extend v1 — significant)

New columns:

```sql
ALTER TABLE assets ADD COLUMN stage_name        TEXT;    -- which pipeline stage produced this
ALTER TABLE assets ADD COLUMN scene_index       INTEGER; -- 0-based scene number (for video clips)
ALTER TABLE assets ADD COLUMN short_id          TEXT;    -- if this asset belongs to a Short
ALTER TABLE assets ADD COLUMN file_size_bytes   INTEGER; -- on-disk file size
ALTER TABLE assets ADD COLUMN duration_ms       INTEGER; -- for video/audio assets
ALTER TABLE assets ADD COLUMN generation_attempt INTEGER DEFAULT 1; -- 1 = first attempt, 2+ = retry
ALTER TABLE assets ADD COLUMN quality_status    TEXT DEFAULT 'unvalidated';
                                                -- "unvalidated" | "gate_passed" | "gate_failed" | "archived"
ALTER TABLE assets ADD COLUMN parent_asset_id   TEXT;    -- for derived assets (e.g. thumbnail from ref image)
ALTER TABLE assets ADD COLUMN generation_params TEXT;    -- JSON: provider-specific params used
ALTER TABLE assets ADD COLUMN content_hash      TEXT;    -- SHA-256 of file content (first 64 chars)
```

**Answers:**
- What assets exist for scene 3 of episode X?
- What was the file size / duration of this clip?
- Was this a first-attempt or retry asset?
- Was this asset validated by a gate?
- What's the lineage of this asset?

---

### 3.6 `asset_versions` (NEW — retry and quality tracking)

One row per generation attempt for each logical asset slot. The `assets` table records the
_final accepted_ asset; `asset_versions` records every attempt including failures.

```sql
CREATE TABLE IF NOT EXISTS asset_versions (
    version_id        TEXT    NOT NULL PRIMARY KEY,  -- UUID4
    asset_slot_id     TEXT    NOT NULL,   -- logical ID for the asset slot (episode_id + type + scene_index)
    run_id            TEXT    NOT NULL,
    episode_id        TEXT    NOT NULL,
    attempt_number    INTEGER NOT NULL,   -- 1-indexed
    asset_id          TEXT,               -- FK to assets (null if this attempt failed)
    provider          TEXT    NOT NULL,
    asset_type        TEXT    NOT NULL,
    prompt_hash       TEXT,
    status            TEXT    NOT NULL,   -- "success" | "failed" | "timeout" | "quota_exceeded"
    error_type        TEXT,               -- error class if status != "success"
    cost_usd          REAL,               -- cost incurred for this attempt (even if failed)
    attempted_at      TEXT    NOT NULL,
    duration_ms       INTEGER,
    metadata_version  TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_versions_slot    ON asset_versions(asset_slot_id);
CREATE INDEX IF NOT EXISTS idx_asset_versions_run     ON asset_versions(run_id);
CREATE INDEX IF NOT EXISTS idx_asset_versions_episode ON asset_versions(episode_id);
```

**Answers:**
- How many attempts were needed to generate this asset?
- What fraction of generation attempts fail? By provider?
- What is the "wasted" cost from failed attempts?
- Which providers have the highest retry rate?

---

### 3.7 `costs` (extend v1)

New columns:

```sql
ALTER TABLE costs ADD COLUMN stage_name      TEXT;    -- pipeline stage that made the call
ALTER TABLE costs ADD COLUMN stage_exec_id   TEXT;    -- FK to stage_executions
ALTER TABLE costs ADD COLUMN asset_id        TEXT;    -- FK to assets (if cost produced an asset)
ALTER TABLE costs ADD COLUMN attempt_number  INTEGER DEFAULT 1; -- 1 = first try
ALTER TABLE costs ADD COLUMN is_retry_waste  INTEGER DEFAULT 0; -- 1 if this was a failed attempt cost
ALTER TABLE costs ADD COLUMN short_id        TEXT;    -- if cost was for a Short deliverable
```

**Answers:**
- What did stage X cost for episode Y?
- How much of our total spend was wasted on failed attempts?
- What is the cost per scene? Per asset type?
- How much did Shorts generation cost vs main episode?

---

### 3.8 `budget_authorizations` (NEW — finance gate records)

One row per Finance Agent authorization gate evaluation.

```sql
CREATE TABLE IF NOT EXISTS budget_authorizations (
    auth_id           TEXT    NOT NULL PRIMARY KEY,  -- UUID4
    run_id            TEXT    NOT NULL,
    episode_id        TEXT    NOT NULL,
    evaluated_at      TEXT    NOT NULL,
    verdict           TEXT    NOT NULL,   -- "AUTHORIZED" | "WARNING" | "BLOCKED"
    budget_ceiling_usd REAL,
    estimated_cost_usd REAL,
    headroom_usd      REAL,               -- ceiling - estimated
    estimates_by_provider TEXT,           -- JSON: {provider: estimated_usd}
    model_slug        TEXT,
    metadata_version  TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
```

**Answers:**
- What was the budget estimate vs actual for each episode?
- How accurate is the Finance Agent's estimates over time?
- How often are runs blocked by the budget gate?

---

### 3.9 `shorts` (NEW — Shorts deliverable tracking)

One row per Short deliverable per episode. Shorts are a distinct unit: they have their own
assets (video, thumbnail, audio), their own performance metrics, and their own gate verdicts.

```sql
CREATE TABLE IF NOT EXISTS shorts (
    short_id          TEXT    NOT NULL PRIMARY KEY,   -- e.g. "ep_001_short_1"
    run_id            TEXT    NOT NULL,
    episode_id        TEXT    NOT NULL,
    short_index       INTEGER NOT NULL,               -- 1-indexed (1, 2, 3)
    title             TEXT,
    status            TEXT    NOT NULL DEFAULT 'in_progress',
                                                      -- "in_progress" | "complete" | "failed"
    video_asset_id    TEXT,                           -- FK to assets
    thumbnail_asset_id TEXT,                          -- FK to assets
    audio_asset_id    TEXT,                           -- FK to assets
    duration_seconds  INTEGER,
    youtube_video_id  TEXT,                           -- set after publishing
    published_at      TEXT,
    created_at        TEXT    NOT NULL,
    metadata_version  TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_shorts_episode ON shorts(episode_id);
CREATE INDEX IF NOT EXISTS idx_shorts_run     ON shorts(run_id);
```

**Answers:**
- How many Shorts were produced for episode X? Were they all completed?
- What YouTube video IDs do the Shorts have?
- Which Short had the highest view count?

---

### 3.10 `render_timelines` (NEW — Assembly Agent output record)

One row per timeline entry (scene-to-clip mapping). Written by Assembly Agent.
Enables post-run audit of exactly what was rendered.

```sql
CREATE TABLE IF NOT EXISTS render_timelines (
    timeline_entry_id TEXT    NOT NULL PRIMARY KEY,  -- UUID4
    run_id            TEXT    NOT NULL,
    episode_id        TEXT    NOT NULL,
    short_id          TEXT,                          -- null for main episode entries
    scene_index       INTEGER NOT NULL,
    clip_index        INTEGER NOT NULL DEFAULT 0,    -- position within scene
    asset_id          TEXT    NOT NULL,              -- video clip asset_id
    narration_asset_id TEXT,                         -- narration beat asset_id
    start_ms          INTEGER NOT NULL DEFAULT 0,    -- clip start in final video
    end_ms            INTEGER,                       -- clip end
    duration_ms       INTEGER,
    padded            INTEGER NOT NULL DEFAULT 0,    -- 1 if clip was padded to fill scene slot
    trimmed           INTEGER NOT NULL DEFAULT 0,    -- 1 if clip was trimmed
    created_at        TEXT    NOT NULL,
    metadata_version  TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_render_timelines_run     ON render_timelines(run_id);
CREATE INDEX IF NOT EXISTS idx_render_timelines_episode ON render_timelines(episode_id);
```

**Answers:**
- What exactly was rendered in this episode (scene-by-scene)?
- Were clips padded or trimmed?
- Can we reproduce the exact timeline for a re-render?

---

### 3.11 `signal_windows` (NEW — analytics time series)

One row per analytics fetch per episode. Where `signal_records` is a per-run summary,
`signal_windows` builds the time series showing how an episode performs over time.

```sql
CREATE TABLE IF NOT EXISTS signal_windows (
    window_id             TEXT    NOT NULL PRIMARY KEY,  -- UUID4
    episode_id            TEXT    NOT NULL,
    youtube_video_id      TEXT    NOT NULL,
    window_start_date     TEXT    NOT NULL,   -- ISO date
    window_end_date       TEXT    NOT NULL,   -- ISO date
    window_days           INTEGER NOT NULL,
    fetched_at            TEXT    NOT NULL,
    views                 REAL,
    watch_minutes         REAL,
    avg_view_duration_s   REAL,
    likes                 REAL,
    comments              REAL,
    shares                REAL,
    subscribers_gained    REAL,
    ctr                   REAL,               -- click-through rate if available
    impressions           REAL,
    data_quality          TEXT    NOT NULL DEFAULT 'no_data',
                                              -- "captured" | "partial" | "error" | "no_data"
    metadata_version      TEXT    NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_signal_windows_episode ON signal_windows(episode_id);
CREATE INDEX IF NOT EXISTS idx_signal_windows_date    ON signal_windows(window_start_date);
```

**Answers:**
- How is episode X performing across its first 7, 14, 28 days?
- Which episodes have the best 7-day retention?
- Is performance improving over time across all episodes?
- What is the correlation between thumbnail style and CTR?

---

### 3.12 `archive_index` (NEW — Archive Recall Layer)

One row per archivable asset with semantic recall metadata. Enables the Archive Recall Layer
to find previously generated assets suitable for reuse or reference.

```sql
CREATE TABLE IF NOT EXISTS archive_index (
    archive_id        TEXT    NOT NULL PRIMARY KEY,  -- UUID4
    asset_id          TEXT    NOT NULL,              -- FK to assets
    episode_id        TEXT    NOT NULL,
    asset_type        TEXT    NOT NULL,
    provider          TEXT    NOT NULL,
    file_path         TEXT,                          -- current on-disk path
    file_size_bytes   INTEGER,
    content_hash      TEXT,                          -- SHA-256 for dedup
    subject_tags      TEXT,                          -- JSON array: ["roman_aqueduct", "underground", "torch"]
    period_tags       TEXT,                          -- JSON array: ["roman", "1st_century_bce"]
    location_tags     TEXT,                          -- JSON array: ["rome", "subterranean"]
    style_tags        TEXT,                          -- JSON array: ["warm_amber", "dramatic_side_lighting"]
    quality_verdict   TEXT,                          -- "gate_passed" | "unvalidated"
    gate_name         TEXT,                          -- which gate approved this (e.g. "tig")
    performance_signal REAL,                         -- episode view count (populated after analytics)
    archived_at       TEXT    NOT NULL,
    metadata_version  TEXT    NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_archive_asset      ON archive_index(asset_id);
CREATE INDEX IF NOT EXISTS idx_archive_episode    ON archive_index(episode_id);
CREATE INDEX IF NOT EXISTS idx_archive_type       ON archive_index(asset_type);
CREATE INDEX IF NOT EXISTS idx_archive_quality    ON archive_index(quality_verdict);
```

**Answers:**
- Have we generated a scene with subject "roman_aqueduct" and style "warm_amber" before?
- What is the best-performing thumbnail from our archive?
- Can we find a reference image that matches this scene description?
- What assets from past episodes could be reused without regeneration?

---

### 3.13 `kg_entities` (NEW — Knowledge Graph entity store)

Extracted from `kg_seeds` for queryable cross-episode entity tracking.

```sql
CREATE TABLE IF NOT EXISTS kg_entities (
    entity_id         TEXT    NOT NULL PRIMARY KEY,   -- from identifiers.generate_entity_id()
    kg_seed_id        TEXT    NOT NULL,               -- FK to kg_seeds
    episode_id        TEXT    NOT NULL,
    name              TEXT    NOT NULL,
    entity_type       TEXT    NOT NULL,               -- "topic" | "person" | "place" | "artefact" | "source"
    description       TEXT,
    first_seen        TEXT    NOT NULL,               -- extracted_at of first kg_seed
    last_seen         TEXT    NOT NULL,               -- extracted_at of most recent kg_seed
    episode_count     INTEGER NOT NULL DEFAULT 1,     -- how many episodes mention this entity
    metadata_version  TEXT    NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_episode ON kg_entities(episode_id);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type    ON kg_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_kg_entities_name    ON kg_entities(name);
```

**Answers:**
- Have we covered the Antikythera Mechanism before?
- Which entities appear across multiple episodes (recurring themes)?
- What topics have we not yet covered that are in our knowledge base?

---

### 3.14 `learning_records` (NEW — Learning Layer)

One row per learning signal per episode. Captures what the factory learned and what decisions
should influence future runs.

```sql
CREATE TABLE IF NOT EXISTS learning_records (
    learning_id       TEXT    NOT NULL PRIMARY KEY,   -- UUID4
    episode_id        TEXT    NOT NULL,
    run_id            TEXT    NOT NULL,
    recorded_at       TEXT    NOT NULL,
    signal_type       TEXT    NOT NULL,               -- "performance" | "gate_outcome" |
                                                      -- "cost_efficiency" | "quality_verdict" |
                                                      -- "topic_score" | "thumbnail_ctr"
    signal_value      REAL,                           -- numeric signal (e.g. view count, CTR)
    signal_label      TEXT,                           -- categorical label (e.g. "above_average")
    context           TEXT,                           -- JSON: relevant context (thumbnail style, topic type, etc.)
    recommendation    TEXT,                           -- free-text: what this signal implies for future runs
    applied_to_run    TEXT,                           -- run_id of first run that applied this learning (if any)
    metadata_version  TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_learning_episode     ON learning_records(episode_id);
CREATE INDEX IF NOT EXISTS idx_learning_signal_type ON learning_records(signal_type);
```

**Answers:**
- What did the factory learn from episode X?
- Which topic types score highest with the EP gate?
- What thumbnail styles correlate with high CTR?
- Has this learning been applied to any subsequent run?

---

## 4. Required Indexes (beyond table-level)

```sql
-- Cross-table analytics
CREATE INDEX IF NOT EXISTS idx_costs_stage_name     ON costs(stage_name);
CREATE INDEX IF NOT EXISTS idx_costs_episode        ON costs(episode_id);
CREATE INDEX IF NOT EXISTS idx_costs_provider       ON costs(provider);
CREATE INDEX IF NOT EXISTS idx_assets_episode_type  ON assets(episode_id, asset_type);
CREATE INDEX IF NOT EXISTS idx_assets_quality       ON assets(quality_status);
CREATE INDEX IF NOT EXISTS idx_runs_episode         ON runs(episode_id);
CREATE INDEX IF NOT EXISTS idx_runs_status          ON runs(status);
CREATE INDEX IF NOT EXISTS idx_episodes_status      ON episodes(status);
```

---

## 5. Required Views (Dashboard Layer)

```sql
-- Episode cost summary (used by dashboard)
CREATE VIEW IF NOT EXISTS v_episode_costs AS
SELECT
    episode_id,
    SUM(cost_usd)                                   AS total_cost_usd,
    COUNT(*)                                        AS call_count,
    SUM(CASE WHEN is_retry_waste = 1 THEN cost_usd ELSE 0 END) AS wasted_cost_usd,
    MAX(called_at)                                  AS last_cost_at
FROM costs
GROUP BY episode_id;

-- Stage performance summary (used by dashboard)
CREATE VIEW IF NOT EXISTS v_stage_performance AS
SELECT
    stage_name,
    COUNT(*)                                        AS total_executions,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS successes,
    SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END) AS failures,
    SUM(CASE WHEN skipped = 1          THEN 1 ELSE 0 END) AS skips,
    AVG(CASE WHEN status = 'completed' THEN duration_ms END) AS avg_duration_ms,
    AVG(cost_usd)                                   AS avg_cost_usd
FROM stage_executions
GROUP BY stage_name;

-- Gate pass rates (used by dashboard)
CREATE VIEW IF NOT EXISTS v_gate_pass_rates AS
SELECT
    gate_name,
    COUNT(*)                                         AS total_evaluations,
    SUM(CASE WHEN verdict IN ('APPROVED', 'APPROVED_WITH_FLAGS') THEN 1 ELSE 0 END) AS passes,
    SUM(CASE WHEN verdict IN ('REJECTED', 'BLOCKED') THEN 1 ELSE 0 END) AS blocks,
    SUM(CASE WHEN verdict = 'WARNING'                THEN 1 ELSE 0 END) AS warnings
FROM gate_verdicts
GROUP BY gate_name;

-- Asset retry rates
CREATE VIEW IF NOT EXISTS v_asset_retry_rates AS
SELECT
    episode_id,
    asset_type,
    provider,
    COUNT(*)                                         AS total_attempts,
    SUM(CASE WHEN attempt_number = 1 THEN 1 ELSE 0 END) AS first_attempts,
    SUM(CASE WHEN attempt_number > 1 THEN 1 ELSE 0 END) AS retry_attempts,
    SUM(CASE WHEN status = 'failed' THEN cost_usd ELSE 0 END) AS wasted_usd
FROM asset_versions
GROUP BY episode_id, asset_type, provider;
```

---

## 6. Migration Strategy

All v1 tables are preserved. v2 is fully backwards-compatible:
- New columns use `ALTER TABLE ... ADD COLUMN` (nullable or DEFAULT)
- New tables use `CREATE TABLE IF NOT EXISTS`
- v1 data is not migrated — new columns will be NULL for existing rows

Migration execution: update `hypogeum/schema.sql` to include all v2 additions, then call
`initialize_schema()` on next pipeline startup (it is idempotent via `IF NOT EXISTS`).

Increment `METADATA_VERSION` to `"v2"` in `hypogeum/identifiers.py` when migration is deployed.

---

## 7. Write Responsibility Map

| Table | Written by | When |
|---|---|---|
| `runs` | `run_registry.py` | Pipeline start/end |
| `episodes` | `pipeline.py` | Pipeline start |
| `stage_executions` | `pipeline.py` | Before/after each stage |
| `gate_verdicts` | `brand_guardian_agent.py`, `thumbnail_identity_guardian.py`, `qa_agent.py`, `compliance_agent.py` | After each verdict |
| `assets` | `image_generation_agent.py`, `video_generation_agent.py`, `voice_generation_operator.py`, `thumbnail_generation_agent.py` | After each generation |
| `asset_versions` | Same as `assets` — every attempt, success or failure | During retry loops |
| `costs` | `finance.py` (via `record_cost()`) | After each billable call |
| `budget_authorizations` | `finance_agent.py` | After authorization gate |
| `thumbnail_candidates` | `thumbnail_director.py` | After planning phase |
| `titles` | `executive_producer.py` | After topic approval |
| `shorts` | `assembly_agent.py` | After Short render |
| `render_timelines` | `assembly_agent.py` | After timeline construction |
| `signal_records` | `signals.py` | After analytics ingestion |
| `signal_windows` | `analytics_ingest.py` | After each analytics fetch |
| `kg_seeds` | `knowledge_graph.py` | After KG extraction |
| `kg_entities` | `knowledge_graph.py` | After KG extraction |
| `archive_index` | `asset_archive_agent.py` | After post-run archiving |
| `learning_records` | `post_run_intelligence.py` | After performance signal ingestion |

---

## 8. Open Questions Before Implementation

1. **`stage_execution_id` threading:** The `stage_exec_id` FK in `gate_verdicts` and `costs`
   requires passing a stage execution ID from `pipeline.py` into agent calls. Current agent
   signatures do not accept this. Implementation must add `stage_exec_id` to agent call
   signatures or pass it via a context object (similar to how `run_id` is handled in `finance.py`).

2. **`asset_versions` write point:** The retry loop in `thumbnail_generation_agent.py` is the
   natural write point, but similar loops exist in `video_generation_agent.py` and
   `image_generation_agent.py`. All must be wired consistently.

3. **`kg_entities` normalization:** Currently `kg_seeds` stores the full KG as a JSON blob.
   Extracting entities to rows requires a migration pass or a new extraction step in
   `knowledge_graph.py`.

4. **`archive_index` tagging:** Subject, period, location, and style tags must be generated
   at archive time. This requires either the agent producing the asset (VPD, TD) to provide
   structured tags in their output schemas, or a tagging step in `asset_archive_agent.py`.
   Current output schemas do not include structured semantic tags.

5. **`signal_windows` backfill:** If `analytics_ingest.py` runs periodically (not just
   once), it must write a new `signal_windows` row per fetch rather than overwriting.
   Current implementation overwrites the snapshot file — need to change to append-per-window.
