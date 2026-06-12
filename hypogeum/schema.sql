-- hypogeum/schema.sql — Operational database schema (SQLite)
--
-- Version: v1 (METADATA_VERSION)
-- Engine:  SQLite 3.x
--
-- All timestamps are ISO 8601 UTC strings (TEXT).
-- All booleans are INTEGER 0/1.
-- All JSON blobs are TEXT (serialized JSON).
-- Foreign key enforcement requires PRAGMA foreign_keys=ON per connection.
--
-- Schema is append-only at v1. New columns may be added with ALTER TABLE
-- ADD COLUMN (nullable or with DEFAULT). Dropping or renaming columns
-- requires a schema migration (increment METADATA_VERSION to "v2").
--
-- Initialize with: CREATE TABLE IF NOT EXISTS (idempotent).

-- ---------------------------------------------------------------------------
-- runs
-- One row per pipeline execution. run_id is the root foreign key for all
-- other tables. status transitions: running -> complete | failed.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT    NOT NULL PRIMARY KEY,
    episode_id          TEXT    NOT NULL,
    started_at          TEXT    NOT NULL,
    completed_at        TEXT,
    status              TEXT    NOT NULL DEFAULT 'running',
    exit_code           INTEGER,
    error_message       TEXT,
    model_assignments   TEXT,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1'
);

-- ---------------------------------------------------------------------------
-- episodes
-- One row per episode. episode_id is caller-provided (e.g. "ep_000").
-- A single episode may have multiple runs (retries); run_id records the
-- most recent run that produced the episode record.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS episodes (
    episode_id          TEXT    NOT NULL PRIMARY KEY,
    title               TEXT,
    topic               TEXT,
    created_at          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'in_progress',
    run_id              TEXT,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- ---------------------------------------------------------------------------
-- assets
-- One row per generated asset. asset_id is deterministic (see identifiers.py).
-- file_path is relative to .hypogeum-assets/.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assets (
    asset_id            TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    asset_type          TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    prompt_hash         TEXT,
    file_path           TEXT,
    created_at          TEXT    NOT NULL,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- ---------------------------------------------------------------------------
-- costs
-- One row per billable API call. cost_id is a UUID generated at record time.
-- Aggregate episode cost = SUM(cost_usd) WHERE episode_id = ?
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS costs (
    cost_id             TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    model               TEXT,
    call_type           TEXT    NOT NULL,
    tokens_or_credits   REAL,
    cost_usd            REAL    NOT NULL,
    called_at           TEXT    NOT NULL,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- ---------------------------------------------------------------------------
-- thumbnail_candidates
-- One row per TD candidate per run. selected=1 marks the rank-1 candidate
-- that was promoted to the final thumbnail.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS thumbnail_candidates (
    candidate_id        TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    rank                INTEGER NOT NULL,
    text_overlay        TEXT,
    placement           TEXT,
    visual_prompt       TEXT,
    selected            INTEGER NOT NULL DEFAULT 0,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- ---------------------------------------------------------------------------
-- titles
-- One row per title candidate per run. selected=1 marks the chosen title.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS titles (
    title_id            TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    title_text          TEXT    NOT NULL,
    selected            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- ---------------------------------------------------------------------------
-- kg_seeds
-- One row per knowledge graph seed extraction per episode.
-- kg_seed_id is deterministic from episode_id + run_id.
-- entity_count / relationship_count / source_count mirror extraction_summary.
-- full_record stores the complete kg_seed JSON for replay/audit.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS kg_seeds (
    kg_seed_id          TEXT    NOT NULL PRIMARY KEY,
    episode_id          TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    extracted_at        TEXT    NOT NULL,
    entity_count        INTEGER NOT NULL DEFAULT 0,
    relationship_count  INTEGER NOT NULL DEFAULT 0,
    source_count        INTEGER NOT NULL DEFAULT 0,
    full_record         TEXT,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1'
);

-- ---------------------------------------------------------------------------
-- signal_records
-- One row per analytics signal ingestion per episode.
-- signal_id is deterministic from episode_id + run_id.
-- views / watch_minutes / data_quality are indexed summary columns.
-- full_record stores the complete signal_record JSON for replay/audit.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signal_records (
    signal_id           TEXT    NOT NULL PRIMARY KEY,
    episode_id          TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    ingested_at         TEXT    NOT NULL,
    youtube_video_id    TEXT,
    window_days         INTEGER NOT NULL DEFAULT 0,
    data_quality        TEXT    NOT NULL DEFAULT 'no_data',
    views               REAL,
    watch_minutes       REAL,
    full_record         TEXT,
    metadata_version    TEXT    NOT NULL DEFAULT 'v1'
);

-- ===========================================================================
-- v2 TABLES
-- Schema version: v2
-- All new tables use CREATE TABLE IF NOT EXISTS (idempotent).
-- Column-level additions to v1 tables are handled via ALTER TABLE in db.py
-- (_apply_v2_column_migrations), not here.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- stage_executions
-- One row per pipeline stage per run. Enables per-stage timing, failure
-- rates, and cost attribution. Written by pipeline.py at stage boundaries.
-- status: "running" | "completed" | "failed" | "skipped"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stage_executions (
    stage_exec_id       TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    stage_name          TEXT    NOT NULL,
    started_at          TEXT    NOT NULL,
    completed_at        TEXT,
    duration_ms         INTEGER,
    status              TEXT    NOT NULL DEFAULT 'running',
    skipped             INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT,
    output_schema       TEXT,
    model_slug          TEXT,
    cost_usd            REAL,
    metadata_version    TEXT    NOT NULL DEFAULT 'v2',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_stage_executions_run     ON stage_executions(run_id);
CREATE INDEX IF NOT EXISTS idx_stage_executions_episode ON stage_executions(episode_id);
CREATE INDEX IF NOT EXISTS idx_stage_executions_stage   ON stage_executions(stage_name);
CREATE INDEX IF NOT EXISTS idx_stage_executions_status  ON stage_executions(status);

-- ---------------------------------------------------------------------------
-- gate_verdicts
-- One row per gate evaluation. Covers BGA Pass 1/2, TIG, QA, Compliance.
-- Enables brand compliance analytics and gate pass-rate trending.
-- verdict: "APPROVED" | "APPROVED_WITH_FLAGS" | "WARNING" | "REJECTED" |
--          "BLOCKED" | "error"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gate_verdicts (
    verdict_id          TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    stage_exec_id       TEXT,
    gate_name           TEXT    NOT NULL,
    verdict             TEXT    NOT NULL,
    evaluated_at        TEXT    NOT NULL,
    asset_id            TEXT,
    rule_ids_flagged    TEXT,
    flag_count          INTEGER NOT NULL DEFAULT 0,
    blocking_count      INTEGER NOT NULL DEFAULT 0,
    warning_count       INTEGER NOT NULL DEFAULT 0,
    model_slug          TEXT,
    full_report         TEXT,
    metadata_version    TEXT    NOT NULL DEFAULT 'v2',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_gate_verdicts_run     ON gate_verdicts(run_id);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_episode ON gate_verdicts(episode_id);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_gate    ON gate_verdicts(gate_name);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_verdict ON gate_verdicts(verdict);
CREATE INDEX IF NOT EXISTS idx_gate_verdicts_asset   ON gate_verdicts(asset_id);

-- ---------------------------------------------------------------------------
-- asset_versions
-- One row per generation attempt per logical asset slot. Tracks every
-- attempt including failures for retry cost accounting.
-- status: "attempted" | "succeeded" | "failed"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS asset_versions (
    version_id          TEXT    NOT NULL PRIMARY KEY,
    asset_slot_id       TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    asset_id            TEXT,
    version_index       INTEGER NOT NULL DEFAULT 0,
    attempt_number      INTEGER NOT NULL DEFAULT 1,
    provider            TEXT    NOT NULL,
    asset_type          TEXT    NOT NULL,
    model               TEXT,
    prompt_hash         TEXT,
    status              TEXT    NOT NULL DEFAULT 'attempted',
    error_type          TEXT,
    error_message       TEXT,
    cost_usd            REAL,
    cost_id             TEXT,
    attempted_at        TEXT    NOT NULL,
    duration_ms         INTEGER,
    metadata_version    TEXT    NOT NULL DEFAULT 'v2',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_versions_slot    ON asset_versions(asset_slot_id);
CREATE INDEX IF NOT EXISTS idx_asset_versions_run     ON asset_versions(run_id);
CREATE INDEX IF NOT EXISTS idx_asset_versions_episode ON asset_versions(episode_id);
CREATE INDEX IF NOT EXISTS idx_asset_versions_status  ON asset_versions(status);

-- ---------------------------------------------------------------------------
-- budget_authorizations
-- One row per Finance Agent authorization gate evaluation per run.
-- verdict: "AUTHORIZED" | "WARNING" | "BLOCKED"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS budget_authorizations (
    auth_id                 TEXT    NOT NULL PRIMARY KEY,
    run_id                  TEXT    NOT NULL,
    episode_id              TEXT    NOT NULL,
    evaluated_at            TEXT    NOT NULL,
    verdict                 TEXT    NOT NULL,
    budget_ceiling_usd      REAL,
    estimated_cost_usd      REAL,
    headroom_usd            REAL,
    estimates_by_provider   TEXT,
    model_slug              TEXT,
    metadata_version        TEXT    NOT NULL DEFAULT 'v2',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- ---------------------------------------------------------------------------
-- shorts
-- One row per Shorts deliverable per episode.
-- status: "in_progress" | "complete" | "failed"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS shorts (
    short_id            TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    short_index         INTEGER NOT NULL,
    title               TEXT,
    status              TEXT    NOT NULL DEFAULT 'in_progress',
    video_asset_id      TEXT,
    thumbnail_asset_id  TEXT,
    audio_asset_id      TEXT,
    duration_seconds    INTEGER,
    youtube_video_id    TEXT,
    published_at        TEXT,
    created_at          TEXT    NOT NULL,
    metadata_version    TEXT    NOT NULL DEFAULT 'v2',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_shorts_episode ON shorts(episode_id);
CREATE INDEX IF NOT EXISTS idx_shorts_run     ON shorts(run_id);

-- ---------------------------------------------------------------------------
-- render_timelines
-- One row per timeline entry (scene-to-clip mapping). Written by Assembly
-- Agent. Enables post-run timeline audit and re-render reproducibility.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS render_timelines (
    timeline_entry_id   TEXT    NOT NULL PRIMARY KEY,
    run_id              TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    short_id            TEXT,
    scene_index         INTEGER NOT NULL,
    clip_index          INTEGER NOT NULL DEFAULT 0,
    asset_id            TEXT    NOT NULL,
    narration_asset_id  TEXT,
    start_ms            INTEGER NOT NULL DEFAULT 0,
    end_ms              INTEGER,
    duration_ms         INTEGER,
    padded              INTEGER NOT NULL DEFAULT 0,
    trimmed             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    metadata_version    TEXT    NOT NULL DEFAULT 'v2',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_render_timelines_run     ON render_timelines(run_id);
CREATE INDEX IF NOT EXISTS idx_render_timelines_episode ON render_timelines(episode_id);

-- ---------------------------------------------------------------------------
-- signal_windows
-- One row per analytics fetch per episode. Builds a time series showing
-- how an episode performs across multiple measurement windows.
-- data_quality: "captured" | "partial" | "error" | "no_data"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signal_windows (
    window_id               TEXT    NOT NULL PRIMARY KEY,
    episode_id              TEXT    NOT NULL,
    youtube_video_id        TEXT    NOT NULL,
    window_start_date       TEXT    NOT NULL,
    window_end_date         TEXT    NOT NULL,
    window_days             INTEGER NOT NULL,
    fetched_at              TEXT    NOT NULL,
    views                   REAL,
    watch_minutes           REAL,
    avg_view_duration_s     REAL,
    likes                   REAL,
    comments                REAL,
    shares                  REAL,
    subscribers_gained      REAL,
    ctr                     REAL,
    impressions             REAL,
    data_quality            TEXT    NOT NULL DEFAULT 'no_data',
    metadata_version        TEXT    NOT NULL DEFAULT 'v2'
);

CREATE INDEX IF NOT EXISTS idx_signal_windows_episode ON signal_windows(episode_id);
CREATE INDEX IF NOT EXISTS idx_signal_windows_date    ON signal_windows(window_start_date);

-- ---------------------------------------------------------------------------
-- archive_index
-- One row per archived asset. Enables Archive Recall Layer: cross-episode
-- asset reuse and best-performing asset lookup.
-- reuse_eligible: 1 if this asset can be reused in future episodes.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS archive_index (
    archive_id          TEXT    NOT NULL PRIMARY KEY,
    asset_id            TEXT    NOT NULL,
    episode_id          TEXT    NOT NULL,
    run_id              TEXT,
    asset_type          TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    model               TEXT,
    file_path           TEXT,
    prompt_hash         TEXT,
    file_size_bytes     INTEGER,
    content_hash        TEXT,
    source_stage        TEXT,
    reuse_eligible      INTEGER NOT NULL DEFAULT 1,
    semantic_tags_json  TEXT,
    visual_style_json   TEXT,
    quality_verdict     TEXT    NOT NULL DEFAULT 'unvalidated',
    gate_name           TEXT,
    performance_signal  REAL,
    archived_at         TEXT    NOT NULL,
    metadata_version    TEXT    NOT NULL DEFAULT 'v2'
);

CREATE INDEX IF NOT EXISTS idx_archive_asset     ON archive_index(asset_id);
CREATE INDEX IF NOT EXISTS idx_archive_episode   ON archive_index(episode_id);
CREATE INDEX IF NOT EXISTS idx_archive_type      ON archive_index(asset_type);
CREATE INDEX IF NOT EXISTS idx_archive_reuse     ON archive_index(reuse_eligible);
CREATE INDEX IF NOT EXISTS idx_archive_quality   ON archive_index(quality_verdict);

-- ---------------------------------------------------------------------------
-- kg_entities
-- One row per extracted knowledge graph entity per episode.
-- Enables cross-episode topic tracking and world bible queries.
-- entity_type: "topic" | "person" | "place" | "artefact" | "source" | "other"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS kg_entities (
    entity_id               TEXT    NOT NULL PRIMARY KEY,
    kg_seed_id              TEXT,
    episode_id              TEXT    NOT NULL,
    run_id                  TEXT    NOT NULL,
    entity_name             TEXT    NOT NULL,
    entity_type             TEXT    NOT NULL DEFAULT 'other',
    source_file             TEXT,
    first_seen_episode_id   TEXT    NOT NULL,
    extracted_at            TEXT    NOT NULL,
    metadata_version        TEXT    NOT NULL DEFAULT 'v2'
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_episode ON kg_entities(episode_id);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type    ON kg_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_kg_entities_name    ON kg_entities(entity_name);

-- ---------------------------------------------------------------------------
-- learning_records
-- One row per learning signal per episode. Stores what the factory learned
-- from each episode to inform future runs.
-- signal_type: "performance" | "gate_outcome" | "cost_efficiency" |
--              "quality_verdict" | "topic_score" | "thumbnail_ctr"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS learning_records (
    learning_id         TEXT    NOT NULL PRIMARY KEY,
    episode_id          TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    recorded_at         TEXT    NOT NULL,
    signal_type         TEXT    NOT NULL,
    signal_value        REAL,
    signal_label        TEXT,
    context             TEXT,
    recommendation      TEXT,
    applied_to_run      TEXT,
    metadata_version    TEXT    NOT NULL DEFAULT 'v2',
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_learning_episode     ON learning_records(episode_id);
CREATE INDEX IF NOT EXISTS idx_learning_signal_type ON learning_records(signal_type);

-- ===========================================================================
-- CROSS-TABLE INDEXES (v1 + v2)
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_costs_episode        ON costs(episode_id);
CREATE INDEX IF NOT EXISTS idx_costs_provider       ON costs(provider);
CREATE INDEX IF NOT EXISTS idx_assets_episode_type  ON assets(episode_id, asset_type);
CREATE INDEX IF NOT EXISTS idx_runs_episode         ON runs(episode_id);
CREATE INDEX IF NOT EXISTS idx_runs_status          ON runs(status);
CREATE INDEX IF NOT EXISTS idx_episodes_status      ON episodes(status);

-- ===========================================================================
-- VIEWS (v2 — dashboard layer)
-- ===========================================================================

-- Episode cost summary
CREATE VIEW IF NOT EXISTS v_episode_costs AS
SELECT
    episode_id,
    SUM(cost_usd)                                                           AS total_cost_usd,
    COUNT(*)                                                                AS call_count,
    SUM(CASE WHEN is_retry_waste = 1 THEN cost_usd ELSE 0 END)             AS wasted_cost_usd,
    MAX(called_at)                                                          AS last_cost_at
FROM costs
GROUP BY episode_id;

-- Stage performance summary
CREATE VIEW IF NOT EXISTS v_stage_performance AS
SELECT
    stage_name,
    COUNT(*)                                                                AS total_executions,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)                  AS successes,
    SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END)                  AS failures,
    SUM(CASE WHEN skipped = 1          THEN 1 ELSE 0 END)                  AS skips,
    AVG(CASE WHEN status = 'completed' THEN duration_ms ELSE NULL END)     AS avg_duration_ms,
    AVG(cost_usd)                                                           AS avg_cost_usd
FROM stage_executions
GROUP BY stage_name;

-- Gate pass rates
CREATE VIEW IF NOT EXISTS v_gate_pass_rates AS
SELECT
    gate_name,
    COUNT(*)                                                                AS total_evaluations,
    SUM(CASE WHEN verdict IN ('APPROVED', 'APPROVED_WITH_FLAGS') THEN 1 ELSE 0 END) AS passes,
    SUM(CASE WHEN verdict IN ('REJECTED', 'BLOCKED') THEN 1 ELSE 0 END)   AS blocks,
    SUM(CASE WHEN verdict = 'WARNING' THEN 1 ELSE 0 END)                   AS warnings
FROM gate_verdicts
GROUP BY gate_name;

-- Asset retry rates
CREATE VIEW IF NOT EXISTS v_asset_retry_rates AS
SELECT
    episode_id,
    asset_type,
    provider,
    COUNT(*)                                                                AS total_attempts,
    SUM(CASE WHEN attempt_number = 1 THEN 1 ELSE 0 END)                   AS first_attempts,
    SUM(CASE WHEN attempt_number > 1 THEN 1 ELSE 0 END)                   AS retry_attempts,
    SUM(CASE WHEN status = 'failed'  THEN COALESCE(cost_usd, 0) ELSE 0 END) AS wasted_usd
FROM asset_versions
GROUP BY episode_id, asset_type, provider;
