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

-- ===========================================================================
-- P1-03: User / Role System
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- users
-- One row per user. role enforces the 6-role model.
-- status: "active" | "disabled"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT NOT NULL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('owner','admin','operator','editor','viewer','client')),
    status      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_email  ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role   ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

-- ---------------------------------------------------------------------------
-- api_keys
-- One row per issued API key. key_hash stores the SHA-256 hex digest.
-- The raw key is never stored — shown once at creation time.
-- status: "active" | "revoked"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT NOT NULL PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    key_hash     TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    last_used_at TEXT,
    expires_at   TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_id  ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_status   ON api_keys(status);

-- ===========================================================================
-- P1-04: Credential Manager / Secret Profiles
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- secret_profiles
-- One row per managed credential. credential_data stored as plaintext (V1).
-- scope: "factory" (shared) | "channel-specific" (per channel_id)
-- status: "active" | "expired" | "revoked" | "testing"
-- credential_type: "api_key" | "oauth2_token" | "webhook_url" | "username_password"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS secret_profiles (
    secret_profile_id  TEXT NOT NULL PRIMARY KEY,
    provider           TEXT NOT NULL,
    label              TEXT NOT NULL,
    credential_type    TEXT NOT NULL CHECK(credential_type IN
                           ('api_key','oauth2_token','webhook_url','username_password')),
    credential_data    TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK(status IN ('active','expired','revoked','testing')),
    scope              TEXT NOT NULL DEFAULT 'factory'
                           CHECK(scope IN ('factory','channel-specific')),
    channel_id         TEXT,
    expires_at         TEXT,
    last_tested_at     TEXT,
    last_used_at       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_secret_profiles_provider  ON secret_profiles(provider);
CREATE INDEX IF NOT EXISTS idx_secret_profiles_scope     ON secret_profiles(scope);
CREATE INDEX IF NOT EXISTS idx_secret_profiles_status    ON secret_profiles(status);
CREATE INDEX IF NOT EXISTS idx_secret_profiles_channel   ON secret_profiles(channel_id);

-- ===========================================================================
-- P2-02: Notification Delivery
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- notification_deliveries
-- One row per delivery attempt (one row per channel per signal).
-- status: "pending" | "sent" | "failed"
-- channel_name: "telegram" | "email"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notification_deliveries (
    delivery_id   TEXT NOT NULL PRIMARY KEY,
    signal_id     TEXT,
    channel_name  TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    subject       TEXT,
    message       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                       CHECK(status IN ('pending','sent','failed')),
    sent_at       TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notif_signal_id   ON notification_deliveries(signal_id);
CREATE INDEX IF NOT EXISTS idx_notif_status      ON notification_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_notif_channel     ON notification_deliveries(channel_name);
CREATE INDEX IF NOT EXISTS idx_notif_created_at  ON notification_deliveries(created_at);

-- ===========================================================================
-- P2-03: Schedule Entity and Scheduler Process
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- schedules
-- One row per configured run schedule.
-- scope:   "factory" (shared) | "channel" (per channel_id)
-- cadence: "daily" | "weekly" | "manual"
-- status:  "active" | "paused" | "disabled"
-- next_run_at: ISO 8601 UTC — when the scheduler will next enqueue a run.
--              NULL for manual cadence until explicitly set.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id      TEXT NOT NULL PRIMARY KEY,
    scope            TEXT NOT NULL DEFAULT 'factory'
                          CHECK(scope IN ('factory','channel')),
    channel_id       TEXT,
    cadence          TEXT NOT NULL DEFAULT 'manual'
                          CHECK(cadence IN ('daily','weekly','manual')),
    status           TEXT NOT NULL DEFAULT 'active'
                          CHECK(status IN ('active','paused','disabled')),
    next_run_at      TEXT,
    last_run_at      TEXT,
    max_runs_per_day INTEGER NOT NULL DEFAULT 1,
    label            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_status      ON schedules(status);
CREATE INDEX IF NOT EXISTS idx_schedules_channel_id  ON schedules(channel_id);
CREATE INDEX IF NOT EXISTS idx_schedules_next_run_at ON schedules(next_run_at);

-- ---------------------------------------------------------------------------
-- scheduled_runs
-- One row per run slot enqueued by the scheduler.
-- Does NOT trigger pipeline execution — the pipeline reads pending rows
-- and executes them independently.
-- status: "pending" | "triggered" | "skipped" | "cancelled"
-- run_id: set by the pipeline when it claims and starts this slot (NULL until then).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scheduled_runs (
    scheduled_run_id TEXT NOT NULL PRIMARY KEY,
    schedule_id      TEXT NOT NULL REFERENCES schedules(schedule_id),
    channel_id       TEXT,
    episode_id       TEXT,
    scheduled_for    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending','triggered','skipped','cancelled')),
    triggered_at     TEXT,
    run_id           TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sched_runs_schedule_id   ON scheduled_runs(schedule_id);
CREATE INDEX IF NOT EXISTS idx_sched_runs_status        ON scheduled_runs(status);
CREATE INDEX IF NOT EXISTS idx_sched_runs_scheduled_for ON scheduled_runs(scheduled_for);
CREATE INDEX IF NOT EXISTS idx_sched_runs_channel_id    ON scheduled_runs(channel_id);

-- ===========================================================================
-- P2-04: Run Queue and Queue Claiming
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- run_queue
-- One row per queued work item. Workers claim rows atomically.
-- The queue is the consumer of scheduled_runs (scheduler output).
-- Queue processor only performs state transitions — never executes pipeline.
--
-- status lifecycle:
--   queued    → claimed    (worker atomically claims via conditional UPDATE)
--   queued    → cancelled  (operator cancels before a worker claims)
--   claimed   → completed  (worker reports success)
--   claimed   → failed     (worker reports failure)
--   claimed   → queued     (worker releases for requeue on crash/timeout)
--
-- priority: lower integer = higher priority (0 = default, -1 = urgent).
-- worker_id: unique per worker process; set at claim time, NULL before claim.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS run_queue (
    queue_id          TEXT NOT NULL PRIMARY KEY,
    scheduled_run_id  TEXT REFERENCES scheduled_runs(scheduled_run_id),
    schedule_id       TEXT,
    channel_id        TEXT,
    episode_id        TEXT,
    priority          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'queued'
                           CHECK(status IN ('queued','claimed','completed','failed','cancelled')),
    worker_id         TEXT,
    queued_at         TEXT NOT NULL,
    claimed_at        TEXT,
    completed_at      TEXT,
    failed_at         TEXT,
    error_message     TEXT,
    created_at        TEXT NOT NULL
);

-- General status lookup (most frequent query path)
CREATE INDEX IF NOT EXISTS idx_run_queue_status
    ON run_queue(status);

-- Claim ordering: priority ASC, then FIFO within same priority, queued items only
CREATE INDEX IF NOT EXISTS idx_run_queue_claim_order
    ON run_queue(priority ASC, queued_at ASC)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_run_queue_worker_id
    ON run_queue(worker_id);

CREATE INDEX IF NOT EXISTS idx_run_queue_channel_id
    ON run_queue(channel_id);

CREATE INDEX IF NOT EXISTS idx_run_queue_scheduled_run_id
    ON run_queue(scheduled_run_id);

-- ===========================================================================
-- CALOG: Channel Activation & Lifecycle Governance
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- channel_state_transitions
-- Immutable audit log — one row per state change.
-- Current channel state is the to_state of the most recent row.
-- Rows are never updated or deleted after insert.
--
-- channel states:
--   draft       — channel record exists; profiles not yet linked
--   configured  — all three sub-profiles linked; not yet activated
--   active      — all activation gates passed; scheduler may enqueue runs
--   paused      — operator-suspended; no new runs; resumes to active
--   blocked     — OIS blocking signal active; no new runs
--   archived    — permanently retired; no further transitions except error
--   error       — unexpected condition; can transition to draft to reset
--
-- triggered_by: "operator" | "system:ois" | "system:calog" | "system:scheduler"
-- gate_results: JSON array of {gate, passed, reason} — set on activation only
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS channel_state_transitions (
    transition_id   TEXT NOT NULL PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    triggered_by    TEXT NOT NULL DEFAULT 'operator',
    reason          TEXT,
    gate_results    TEXT,
    transitioned_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cst_channel_id      ON channel_state_transitions(channel_id);
CREATE INDEX IF NOT EXISTS idx_cst_transitioned_at ON channel_state_transitions(channel_id, transitioned_at DESC);

-- ===========================================================================
-- Topic Backlog / Episode Queue
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- episode_topics
-- One row per topic idea waiting to be produced.
-- The Queue Consumer claims the next topic for a channel before calling
-- run_pipeline(). On successful pipeline completion the topic is consumed.
--
-- status lifecycle:
--   queued   → claimed   (consumer atomically claims via conditional UPDATE)
--   claimed  → consumed  (consumer marks after successful pipeline run)
--   claimed  → queued    (future: release on pipeline failure if desired)
--   queued   → skipped   (operator decides to skip this topic)
--   queued   → cancelled (operator cancels before claim)
--   claimed  → cancelled (operator force-cancels an in-flight topic)
--
-- priority: lower integer = higher priority. FIFO within same priority band.
-- reason:   human-readable note for skip/cancel actions.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS episode_topics (
    topic_id    TEXT NOT NULL PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    topic_text  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                     CHECK(status IN ('queued','claimed','consumed','skipped','cancelled')),
    priority    INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    claimed_at  TEXT,
    consumed_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Primary lookup: all queued topics for a channel
CREATE INDEX IF NOT EXISTS idx_ep_topics_channel_status
    ON episode_topics(channel_id, status);

-- Claim ordering: priority ASC then FIFO, queued items only
CREATE INDEX IF NOT EXISTS idx_ep_topics_claim_order
    ON episode_topics(channel_id, priority ASC, created_at ASC)
    WHERE status = 'queued';

-- ===========================================================================
-- P3-02: Factory Entity System
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- factories
-- One row per factory instance. A factory hosts one or more channels.
-- owner_id references users(user_id) when P1-03 user record exists; nullable
-- to allow bootstrap before owner user is created.
-- status: "active" | "maintenance" | "suspended"
-- factory_policies: reserved for P3-05 policy cascade (JSON blob, added via
--   _V2_COLUMN_MIGRATIONS["factories"] when P3-05 is implemented).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS factories (
    factory_id       TEXT NOT NULL PRIMARY KEY,
    factory_name     TEXT NOT NULL,
    factory_slug     TEXT NOT NULL UNIQUE,
    owner_id         TEXT REFERENCES users(user_id),
    default_language TEXT NOT NULL DEFAULT 'en',
    default_timezone TEXT NOT NULL DEFAULT 'UTC',
    status           TEXT NOT NULL DEFAULT 'active'
                         CHECK(status IN ('active','maintenance','suspended')),
    dashboard_title  TEXT,
    support_email    TEXT,
    primary_color    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_factories_slug   ON factories(factory_slug);
CREATE INDEX IF NOT EXISTS idx_factories_status ON factories(status);
