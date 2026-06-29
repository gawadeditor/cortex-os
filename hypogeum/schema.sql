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
    created_at        TEXT NOT NULL,
    cancel_requested  INTEGER NOT NULL DEFAULT 0
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

-- ===========================================================================
-- P3-03: Run Lifecycle Governance (RLG) — State Machine
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- run_state_transitions
-- Immutable audit trail of every run state change. One row per transition.
-- Rows are INSERT-only — no UPDATE or DELETE paths exist in the API.
-- from_state is NULL for the initial registration transition (None → queued
-- or None → running when register_run skips the queued phase).
-- triggered_by: user_id or system actor (e.g. "system:pipeline").
-- trigger_source: component that called transition_run_state
--                 (e.g. "run_registry", "pipeline", "system:ois").
-- metadata: optional JSON blob for context (e.g. error type, approval_id).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS run_state_transitions (
    transition_id   TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,
    trigger_source  TEXT NOT NULL,
    reason          TEXT,
    metadata        TEXT,
    transitioned_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rst_run_id ON run_state_transitions(run_id);
CREATE INDEX IF NOT EXISTS idx_rst_transitioned_at ON run_state_transitions(run_id, transitioned_at);

-- ===========================================================================
-- P3-04: Approval System
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- approvals
-- One row per approval request. Immutable after creation except for the
-- status/decided_* fields which are set once when the request is decided.
-- approval_type:
--   human_review        — operator reviews before publish (channel policy)
--   publish_approval    — explicit approve-to-publish gate
--   model_swap          — policy gate: changing model slug requires approval
--   budget_increase     — policy gate: raising ceiling requires approval
-- status: pending | approved | rejected | expired
-- requested_by: user_id or "system:pipeline"
-- decided_by:   user_id of the operator who approved/rejected
-- evidence:     optional JSON blob — e.g. compliance_report excerpt
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    episode_id      TEXT NOT NULL,
    approval_type   TEXT NOT NULL
                        CHECK(approval_type IN
                            ('human_review','publish_approval',
                             'model_swap','budget_increase')),
    requested_by    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected','expired')),
    requested_at    TEXT NOT NULL,
    decided_by      TEXT,
    decided_at      TEXT,
    notes           TEXT,
    evidence        TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status  ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_type    ON approvals(approval_type, status);

-- ===========================================================================
-- Phase E: Analytics & Learning
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- platform_analytics
-- One row per episode × platform × snapshot_date.
-- Stores normalised metrics regardless of source (manual paste, CSV, API).
-- Multiple snapshots per episode are supported: 7d, 28d, 90d windows.
-- UNIQUE(episode_id, platform, snapshot_date) enables idempotent import.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS platform_analytics (
    record_id          TEXT    NOT NULL PRIMARY KEY,
    episode_id         TEXT    NOT NULL,
    platform           TEXT    NOT NULL
                               CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    snapshot_date      TEXT    NOT NULL,
    window_days        INTEGER NOT NULL DEFAULT 7,
    -- Normalised metrics (NULL = not applicable or not imported for this platform)
    views              INTEGER,   -- plays/views
    impressions        INTEGER,   -- impressions/reach depending on platform
    ctr                REAL,      -- click-through rate 0.0–1.0
    avg_view_pct       REAL,      -- retention proxy 0.0–1.0
    avg_watch_time_s   REAL,      -- average watch time in seconds
    total_watch_time_s REAL,      -- total accumulated watch time in seconds
    likes              INTEGER,
    comments           INTEGER,
    shares             INTEGER,
    saves              INTEGER,   -- bookmarks/saves (Instagram, TikTok)
    platform_video_id  TEXT,      -- platform's own video identifier
    raw_json           TEXT,      -- JSON blob for platform-specific extra fields
    source             TEXT    NOT NULL DEFAULT 'manual'
                               CHECK(source IN ('manual','api','csv','mock')),
    imported_at        TEXT    NOT NULL,
    metadata_version   TEXT    NOT NULL DEFAULT 'v1',
    UNIQUE(episode_id, platform, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_pa_episode  ON platform_analytics(episode_id);
CREATE INDEX IF NOT EXISTS idx_pa_platform ON platform_analytics(platform);
CREATE INDEX IF NOT EXISTS idx_pa_date     ON platform_analytics(snapshot_date DESC);

-- ---------------------------------------------------------------------------
-- analytics_insights
-- Derived signals extracted from platform_analytics by the learning engine.
-- Rule-based on configurable thresholds; one row per fired rule per episode.
-- signal_strength: -1.0 (worst) to +1.0 (best).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analytics_insights (
    insight_id       TEXT    NOT NULL PRIMARY KEY,
    episode_id       TEXT    NOT NULL,
    platform         TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    insight_type     TEXT    NOT NULL,  -- weak_thumbnail|high_retention|hook_failure|…
    affected_dim     TEXT    NOT NULL,  -- thumbnail|title|hook|pacing|topic|
                                        -- publishing_time|duration|description
    signal_strength  REAL    NOT NULL,  -- -1.0 to +1.0
    threshold_used   TEXT,              -- human-readable rule that fired
    detail           TEXT,              -- explanation sentence
    metadata_version TEXT    NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_ai_episode ON analytics_insights(episode_id);
CREATE INDEX IF NOT EXISTS idx_ai_dim     ON analytics_insights(affected_dim);
CREATE INDEX IF NOT EXISTS idx_ai_type    ON analytics_insights(insight_type);

-- ---------------------------------------------------------------------------
-- learning_actions
-- Concrete factory-level changes derived from analytics_insights.
-- Applied actions will be consumed by the pipeline in Phase F.
-- Lifecycle: pending → applied | dismissed
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS learning_actions (
    action_id        TEXT    NOT NULL PRIMARY KEY,
    created_at       TEXT    NOT NULL,
    source_episode   TEXT,               -- episode that triggered this action
    dimension        TEXT    NOT NULL,   -- thumbnail|title|hook|pacing|topic|
                                         -- publishing_time|duration|description
    action_type      TEXT    NOT NULL
                             CHECK(action_type IN
                                 ('prefer','avoid','increase','decrease','test','change')),
    rationale        TEXT    NOT NULL,
    evidence_json    TEXT,               -- JSON: {insight_ids, metrics_snapshot}
    status           TEXT    NOT NULL DEFAULT 'pending'
                             CHECK(status IN ('pending','applied','dismissed')),
    applied_at       TEXT,
    dismissed_at     TEXT,
    metadata_version TEXT    NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_la_status    ON learning_actions(status);
CREATE INDEX IF NOT EXISTS idx_la_dimension ON learning_actions(dimension);
CREATE INDEX IF NOT EXISTS idx_la_episode   ON learning_actions(source_episode);

-- ---------------------------------------------------------------------------
-- Phase E Views
-- ---------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_platform_kpis AS
SELECT
    platform,
    COUNT(DISTINCT episode_id)                     AS episodes_tracked,
    COUNT(*)                                       AS total_snapshots,
    ROUND(AVG(ctr), 4)                            AS avg_ctr,
    ROUND(COALESCE(AVG(avg_view_pct), 0.0), 4)   AS avg_view_pct,
    ROUND(COALESCE(AVG(avg_watch_time_s), 0.0), 1) AS avg_watch_time_s,
    COALESCE(SUM(views), 0)                       AS total_views,
    COALESCE(SUM(total_watch_time_s), 0)          AS total_watch_time_s
FROM platform_analytics
GROUP BY platform;

CREATE VIEW IF NOT EXISTS v_episode_performance AS
SELECT
    pa.episode_id,
    e.title                                    AS episode_title,
    e.topic                                    AS episode_topic,
    e.created_at                               AS episode_created_at,
    COUNT(DISTINCT pa.platform)               AS platforms_tracked,
    COUNT(*)                                   AS total_snapshots,
    ROUND(COALESCE(AVG(pa.ctr), 0.0), 4)     AS avg_ctr,
    ROUND(COALESCE(AVG(pa.avg_view_pct),0.0),4) AS avg_view_pct,
    COALESCE(SUM(pa.views), 0)               AS total_views,
    COALESCE(SUM(pa.total_watch_time_s), 0)  AS total_watch_time_s,
    COUNT(DISTINCT ai.insight_id)              AS insight_count
FROM platform_analytics pa
LEFT JOIN episodes          e  ON e.episode_id  = pa.episode_id
LEFT JOIN analytics_insights ai ON ai.episode_id = pa.episode_id
GROUP BY pa.episode_id;

CREATE VIEW IF NOT EXISTS v_learning_action_summary AS
SELECT
    dimension,
    COUNT(*)                                               AS total_actions,
    SUM(CASE WHEN status='pending'   THEN 1 ELSE 0 END)  AS pending_count,
    SUM(CASE WHEN status='applied'   THEN 1 ELSE 0 END)  AS applied_count,
    SUM(CASE WHEN status='dismissed' THEN 1 ELSE 0 END)  AS dismissed_count
FROM learning_actions
GROUP BY dimension;

-- ===========================================================================
-- F2: Decision Inputs Layer
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- publishing_outcomes
-- One row per episode × platform publishing attempt.
-- Written by store.record_publishing_outcome() — never by live publishing code.
-- publishing_status: 'success' | 'failed' | 'pending' | 'skipped' | 'unknown'
-- UNIQUE(episode_id, platform) — one canonical outcome per episode/platform pair.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publishing_outcomes (
    outcome_id          TEXT NOT NULL PRIMARY KEY,
    episode_id          TEXT NOT NULL,
    run_id              TEXT,
    platform            TEXT NOT NULL,
    publishing_status   TEXT NOT NULL DEFAULT 'unknown'
                             CHECK(publishing_status IN
                                 ('success','failed','pending','skipped','unknown')),
    platform_video_id   TEXT,
    published_at        TEXT,
    url                 TEXT,
    failure_reason      TEXT,
    recorded_at         TEXT NOT NULL,
    metadata_version    TEXT NOT NULL DEFAULT 'v1',
    UNIQUE(episode_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_pub_outcomes_episode  ON publishing_outcomes(episode_id);
CREATE INDEX IF NOT EXISTS idx_pub_outcomes_platform ON publishing_outcomes(platform);
CREATE INDEX IF NOT EXISTS idx_pub_outcomes_status   ON publishing_outcomes(publishing_status);

-- ---------------------------------------------------------------------------
-- decision_reviews  (F4)
-- One row per persisted decision proposal. operator_status tracks the
-- human review lifecycle: pending → accepted | rejected.
-- Accept/reject only records intent — no execution is triggered.
-- decision_id is UNIQUE so re-sync is idempotent.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS decision_reviews (
    review_id          TEXT NOT NULL PRIMARY KEY,
    decision_id        TEXT NOT NULL UNIQUE,
    decision_type      TEXT NOT NULL,
    decision_title     TEXT NOT NULL,
    recommendation     TEXT NOT NULL,
    evidence_json      TEXT,
    confidence         REAL,
    operator_status    TEXT NOT NULL DEFAULT 'pending'
                            CHECK(operator_status IN ('pending','accepted','rejected')),
    operator_note      TEXT,
    created_at         TEXT NOT NULL,
    reviewed_at        TEXT,
    metadata_version   TEXT NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_dr_decision_id ON decision_reviews(decision_id);
CREATE INDEX IF NOT EXISTS idx_dr_status      ON decision_reviews(operator_status);
CREATE INDEX IF NOT EXISTS idx_dr_type        ON decision_reviews(decision_type);

-- ---------------------------------------------------------------------------
-- decision_execution_plans  (F5)
-- Dry-run / reviewable execution plans derived from accepted decision_reviews.
-- Accepting a plan does NOT trigger execution — plan_status='executed' is
-- unreachable in F5. execution_mode is always 'dry_run' in F5.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS decision_execution_plans (
    plan_id             TEXT NOT NULL PRIMARY KEY,
    review_id           TEXT NOT NULL,
    decision_id         TEXT NOT NULL,
    decision_type       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    plan_status         TEXT NOT NULL DEFAULT 'draft'
                             CHECK(plan_status IN ('draft','approved','rejected','executed','cancelled')),
    execution_mode      TEXT NOT NULL DEFAULT 'dry_run'
                             CHECK(execution_mode IN ('dry_run','live')),
    action_summary      TEXT NOT NULL,
    planned_steps_json  TEXT NOT NULL,
    risk_level          TEXT NOT NULL
                             CHECK(risk_level IN ('low','medium','high','blocked')),
    blockers_json       TEXT,
    evidence_json       TEXT,
    operator_note       TEXT,
    approved_at         TEXT,
    rejected_at         TEXT,
    executed_at         TEXT,
    metadata_version    TEXT NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_dep_review_id    ON decision_execution_plans(review_id);
CREATE INDEX IF NOT EXISTS idx_dep_decision_id  ON decision_execution_plans(decision_id);
CREATE INDEX IF NOT EXISTS idx_dep_plan_status  ON decision_execution_plans(plan_status);
CREATE INDEX IF NOT EXISTS idx_dep_decision_type ON decision_execution_plans(decision_type);

-- ---------------------------------------------------------------------------
-- channel_profile_settings — F9: Channel Control Center
-- Single-row table (profile_id = 'default').  get_channel_profile() inserts
-- the default row on first access so the row always exists after that call.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_profile_settings (
    profile_id          TEXT NOT NULL PRIMARY KEY DEFAULT 'default',
    channel_name        TEXT,
    logo_path           TEXT,
    description         TEXT,
    primary_color       TEXT NOT NULL DEFAULT '#3fb950',
    secondary_color     TEXT NOT NULL DEFAULT '#58a6ff',
    accent_color        TEXT NOT NULL DEFAULT '#d29922',
    content_niche       TEXT,
    tone_style          TEXT,
    yt_videos_per_week  INTEGER NOT NULL DEFAULT 2,
    yt_shorts_per_week  INTEGER NOT NULL DEFAULT 3,
    tiktok_per_week     INTEGER NOT NULL DEFAULT 5,
    instagram_per_week  INTEGER NOT NULL DEFAULT 3,
    facebook_per_week   INTEGER NOT NULL DEFAULT 2,
    created_at          TEXT,
    updated_at          TEXT,
    metadata_version    TEXT NOT NULL DEFAULT 'v1'
);

-- ===========================================================================
-- F15: Provider Verifications
-- One row per provider (UPSERT). Stores the most recent verification result
-- so the dashboard can distinguish Missing / Saved / Verified / Failed /
-- Not verifiable without making a live API call on every page load.
--
-- status: 'verified'        — last probe succeeded
--         'failed'          — last probe returned error or non-2xx
--         'not_verifiable'  — no probe implementation for this provider
--         'pending'         — credential exists but never tested
-- source: 'env'             — credential came from environment variable
--         'secret_profile'  — credential came from secret_profiles table
-- ===========================================================================

CREATE TABLE IF NOT EXISTS provider_verifications (
    provider      TEXT NOT NULL PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK(status IN ('verified','failed','not_verifiable','pending')),
    checked_at    TEXT NOT NULL,
    latency_ms    INTEGER,
    error_message TEXT,
    source        TEXT NOT NULL DEFAULT 'env'
                      CHECK(source IN ('env','secret_profile'))
);

-- ===========================================================================
-- provider_comparison_runs — Phase 1G
-- One row per operator-triggered thumbnail provider comparison.
-- results_json holds a JSON array of per-provider result objects:
--   [{provider, model, status, latency_ms, estimated_cost_usd,
--     output_path, error, credential_source}, ...]
-- ===========================================================================
CREATE TABLE IF NOT EXISTS provider_comparison_runs (
    run_id       TEXT NOT NULL PRIMARY KEY,
    run_at       TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running'
                     CHECK(status IN ('running','completed','failed')),
    results_json TEXT,
    completed_at TEXT
);

-- ===========================================================================
-- topic_candidates — Autonomous Discovery (Dashboard Refactor)
-- One row per candidate topic produced by a discovery cycle.
-- cycle_id groups all candidates from the same run.
-- selected=1 marks the system-ranked winner within a cycle.
-- status transitions: pending → approved (operator) | rejected (operator/auto)
--                     approved → consumed (after enqueue_topic completes)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS topic_candidates (
    candidate_id         TEXT NOT NULL PRIMARY KEY,
    channel_id           TEXT NOT NULL,
    cycle_id             TEXT NOT NULL,
    topic                TEXT NOT NULL,
    source               TEXT NOT NULL DEFAULT 'heuristic',
    score                REAL NOT NULL DEFAULT 0.0,
    ctr_rationale        TEXT,
    retention_rationale  TEXT,
    watch_time_rationale TEXT,
    risk                 TEXT,
    selected             INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'pending'
                             CHECK(status IN ('pending','approved','rejected','consumed')),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topiccand_channel ON topic_candidates(channel_id, status);
CREATE INDEX IF NOT EXISTS idx_topiccand_cycle   ON topic_candidates(cycle_id);

-- ===========================================================================
-- discovery_state — Persistent run state for the /discover dashboard page
-- One row per channel_id. Engine-agnostic: both v1 (topic_candidates) and
-- v2 (opportunities) engines write here after each cycle.
-- candidates_json: JSON array of normalized candidate dicts for the template.
-- status lifecycle: idle → running → completed | failed
-- ===========================================================================
CREATE TABLE IF NOT EXISTS discovery_state (
    channel_id       TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'idle'
                         CHECK(status IN ('idle','running','completed','failed')),
    last_cycle_id    TEXT,
    candidate_count  INTEGER NOT NULL DEFAULT 0,
    candidates_json  TEXT,
    started_at       TEXT,
    completed_at     TEXT,
    error            TEXT,
    updated_at       TEXT NOT NULL
);

-- ===========================================================================
-- platform_sync_log — Per-platform auto-sync audit trail
-- One row per sync attempt. triggered_by: 'auto' | 'manual'.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS platform_sync_log (
    sync_id          TEXT NOT NULL PRIMARY KEY,
    platform         TEXT NOT NULL,
    channel_id       TEXT,
    status           TEXT NOT NULL
                         CHECK(status IN ('ok','error','not_connected','skipped')),
    records_imported INTEGER NOT NULL DEFAULT 0,
    errors_json      TEXT,
    window_days      INTEGER NOT NULL DEFAULT 90,
    triggered_by     TEXT NOT NULL DEFAULT 'auto',
    synced_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_platformsync_platform ON platform_sync_log(platform, synced_at);

-- ===========================================================================
-- opportunities — Opportunity registry (Discovery Director v2)
-- opportunity_id is the primary key. canonical_subject is an indexed field —
-- the same subject may generate multiple opportunities over time with
-- different rankings, verification outcomes, and feedback history.
-- Opportunity lifecycle and subject identity are separate concepts.
-- v1 heuristic cycles do not write here.
--
-- Lifecycle:
--   active → selected → enqueued → consumed
--         ↘ rejected | blacklisted | exhausted
-- ===========================================================================
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id          TEXT NOT NULL PRIMARY KEY,
    canonical_subject       TEXT NOT NULL,
    -- Content (from Topic Ideator, six approved string fields)
    topic_text              TEXT NOT NULL,
    angle                   TEXT,
    mystery_hook            TEXT,
    differentiation_claim   TEXT,
    visual_signal           TEXT,
    evidence_signal         TEXT,
    -- Provenance
    generation_source       TEXT,           -- 'llm_ideation' | 'fallback_seed'
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    -- Verification
    verification_status     TEXT NOT NULL DEFAULT 'unverified'
                                CHECK(verification_status IN
                                      ('unverified','verified','failed','skipped')),
    source_count            INTEGER NOT NULL DEFAULT 0,
    source_score            REAL,
    verify_count            INTEGER NOT NULL DEFAULT 0,
    -- Scoring (all populated by initial_scorer.py and source verification)
    initial_composite       REAL,
    final_composite         REAL,
    priority_score          REAL,
    priority_tier           TEXT
                                CHECK(priority_tier IN ('tier1','tier2','tier3',NULL)),
    -- Lifecycle
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK(status IN
                                      ('active','selected','enqueued','consumed',
                                       'rejected','blacklisted','exhausted')),
    rejection_reason        TEXT,
    topic_id                TEXT,   -- episode_topics.topic_id once enqueued
    routed_to               TEXT,   -- channel_id this opportunity was routed to
    selected_at             TEXT,
    enqueued_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_opp_subject  ON opportunities(canonical_subject, status);
CREATE INDEX IF NOT EXISTS idx_opp_status   ON opportunities(status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_opp_priority ON opportunities(priority_tier, priority_score);

-- ===========================================================================
-- opportunity_events — Audit log for opportunity lifecycle transitions
-- Events belong to an opportunity, not to the subject namespace.
-- event_type lifecycle: discovered → angle_added | verified | verification_failed
--                                  → rejected | consumed
-- ===========================================================================
CREATE TABLE IF NOT EXISTS opportunity_events (
    event_id          TEXT NOT NULL PRIMARY KEY,
    opportunity_id    TEXT NOT NULL,
    event_type        TEXT NOT NULL
                          CHECK(event_type IN ('discovered','angle_added','verified',
                                               'verification_failed','rejected','consumed')),
    cycle_id          TEXT,
    channel_id        TEXT,
    detail_json       TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opev_opportunity ON opportunity_events(opportunity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_opev_cycle       ON opportunity_events(cycle_id);

-- ===========================================================================
-- discovery_cycles — One row per v2 Discovery Director cycle.
-- Primary observability surface for the Discovery Director.
-- v1 heuristic cycles do not write here; they appear in topic_candidates only.
-- engine_used: 'v2' | 'v1_fallback'
-- status sealed to 'complete' | 'failed' | 'fallback' on exit.
-- Funnel: raw_candidates_count → viable_after_feasibility → verify_eligible_count
--         → verified_count (source confirmed) vs rejected_source_count
--         → final selection vs rejected_score_count
-- ===========================================================================
CREATE TABLE IF NOT EXISTS discovery_cycles (
    cycle_id                 TEXT NOT NULL PRIMARY KEY,
    channel_id               TEXT NOT NULL,
    engine_used              TEXT NOT NULL DEFAULT 'v2',
    -- Funnel metrics (populated as the cycle progresses)
    raw_candidates_count     INTEGER NOT NULL DEFAULT 0,  -- total returned by Topic Ideator
    viable_after_feasibility INTEGER NOT NULL DEFAULT 0,  -- after basic feasibility filter
    verify_eligible_count    INTEGER NOT NULL DEFAULT 0,  -- above DISCOVERY_VERIFY_THRESHOLD
    verified_count           INTEGER NOT NULL DEFAULT 0,  -- source verification returned true
    rejected_source_count    INTEGER NOT NULL DEFAULT 0,  -- failed source verification
    rejected_score_count     INTEGER NOT NULL DEFAULT 0,  -- score below threshold post-verify
    -- Outcome
    selected_opportunity_id    TEXT,   -- opportunities.opportunity_id
    selected_canonical_subject TEXT,   -- canonical_subject of the selected opportunity
    topic_id_enqueued          TEXT,   -- episode_topics.topic_id if auto-approved
    -- Routing metadata
    model_used               TEXT,                        -- LLM model slug used for ideation
    prompt_version           TEXT,                        -- ideation prompt version tag
    -- Status
    status                   TEXT NOT NULL DEFAULT 'running'
                                 CHECK(status IN ('running','complete','failed','fallback')),
    created_at               TEXT NOT NULL,
    completed_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_disccycle_channel ON discovery_cycles(channel_id, created_at);

-- ===========================================================================
-- source_verifications — Per-canonical_subject verification results (v2 only)
-- Multiple rows per subject are permitted (re-verification across cycles).
-- confidence: 'high' | 'medium' | 'low' | NULL (unverified)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS source_verifications (
    verification_id   TEXT NOT NULL PRIMARY KEY,
    canonical_subject TEXT NOT NULL,
    cycle_id          TEXT,
    verified          INTEGER NOT NULL DEFAULT 0,
    source_url        TEXT,
    confidence        TEXT,
    penalty_applied   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_srcverif_subject ON source_verifications(canonical_subject, created_at);

-- ===========================================================================
-- model_comparison_runs — Model Output Comparison (Step D)
-- One row per operator-initiated comparison session. Results stored as a
-- JSON blob in results_json, matching the provider_comparison_runs pattern.
-- Separate result rows are deferred to v2.
--
-- run_type: 'llm' | 'image' | 'video'
-- status:   'running' | 'completed' | 'cancelled' | 'error'
-- ===========================================================================
CREATE TABLE IF NOT EXISTS model_comparison_runs (
    run_id           TEXT NOT NULL PRIMARY KEY,
    run_type         TEXT NOT NULL
                         CHECK(run_type IN ('llm','image','video')),
    prompt_name      TEXT,
    prompt_text      TEXT NOT NULL,
    input_context    TEXT,
    provider_matrix  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running'
                         CHECK(status IN ('running','completed','cancelled','error')),
    initiated_at     TEXT NOT NULL,
    completed_at     TEXT,
    operator_note    TEXT,
    results_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcrun_status   ON model_comparison_runs(status, initiated_at);

-- ---------------------------------------------------------------------------
-- publish_calendar  (P12-21)
-- One row per episode (or short) per platform representing a planned publish
-- event. Separates asset production timing from live-publishing timing.
--
-- upload_status:  'pending' → 'uploaded' | 'failed' | 'skipped'
-- publish_status: 'pending' → 'scheduled' | 'live' | 'failed' | 'skipped'
-- publish_method: 'api_scheduled' — platform API sets publishAt (YouTube, Meta)
--                 'due_time_upload' — upload must happen at target_publish_at (TikTok)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publish_calendar (
    calendar_id         TEXT NOT NULL PRIMARY KEY,
    episode_id          TEXT NOT NULL,
    short_id            TEXT,           -- NULL for main episode, short_id for Shorts
    platform            TEXT NOT NULL
                            CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    target_publish_at   TEXT NOT NULL,  -- ISO 8601 UTC — when the video should go live
    upload_status       TEXT NOT NULL DEFAULT 'pending'
                            CHECK(upload_status IN ('pending','uploaded','failed','skipped')),
    publish_status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK(publish_status IN ('pending','scheduled','live','failed','skipped')),
    platform_video_id   TEXT,           -- set after successful upload
    platform_supports_scheduled_publish INTEGER NOT NULL DEFAULT 0,
    publish_method      TEXT NOT NULL DEFAULT 'due_time_upload'
                            CHECK(publish_method IN ('api_scheduled','due_time_upload')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(episode_id, short_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_pub_cal_episode  ON publish_calendar(episode_id);
CREATE INDEX IF NOT EXISTS idx_pub_cal_platform ON publish_calendar(platform);
CREATE INDEX IF NOT EXISTS idx_pub_cal_target   ON publish_calendar(target_publish_at);
CREATE INDEX IF NOT EXISTS idx_pub_cal_upload   ON publish_calendar(upload_status)
    WHERE upload_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_pub_cal_publish  ON publish_calendar(publish_status)
    WHERE publish_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_mcrun_type     ON model_comparison_runs(run_type, initiated_at);

-- ---------------------------------------------------------------------------
-- releases  (P12-22)
-- One row per planned publication event for an episode (or short series).
-- Groups the per-platform publish_calendar rows under a single release intent.
--
-- Relationship chain:
--   episode_topics.topic_id  (idea/backlog)
--     → episodes.episode_id  (produced asset package; episodes.topic_id added via migration)
--       → releases.release_id  (planned publication event)
--         → publish_calendar.calendar_id  (per-platform upload/schedule/live job)
--
-- status lifecycle:
--   planned    — release intent registered; assets not yet ready
--   ready      — all episode assets confirmed; publish jobs may begin
--   publishing — at least one platform job is in flight
--   live       — all platform jobs complete/scheduled
--   failed     — at least one platform job failed; may be partial
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS releases (
    release_id          TEXT NOT NULL PRIMARY KEY,
    episode_id          TEXT NOT NULL,
    topic_id            TEXT,            -- FK → episode_topics.topic_id (may be NULL for historical)
    target_publish_at   TEXT NOT NULL,   -- ISO 8601 UTC — intended publish time across platforms
    status              TEXT NOT NULL DEFAULT 'planned'
                            CHECK(status IN ('planned','ready','publishing','live','failed')),
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_releases_episode    ON releases(episode_id);
CREATE INDEX IF NOT EXISTS idx_releases_topic      ON releases(topic_id);
CREATE INDEX IF NOT EXISTS idx_releases_target     ON releases(target_publish_at);
CREATE INDEX IF NOT EXISTS idx_releases_status     ON releases(status);

-- ---------------------------------------------------------------------------
-- finance_approvals — operator-granted single-use overrides for BLOCKED runs
-- ---------------------------------------------------------------------------
-- One row per approval. The pipeline checks this table before raising
-- FinancePostVPDBlockedError. A valid (non-expired, non-consumed) approval
-- lets the run proceed past the budget ceiling for this episode only.
--
-- Lifecycle: pending → consumed (run completed) | expired (24h elapsed)

CREATE TABLE IF NOT EXISTS finance_approvals (
    approval_id             TEXT    NOT NULL PRIMARY KEY,
    episode_id              TEXT    NOT NULL,
    run_id                  TEXT,           -- run that was blocked; NULL if approved proactively
    approved_at             TEXT    NOT NULL,
    approved_by             TEXT    NOT NULL DEFAULT 'operator',
    estimated_spend_usd     REAL    NOT NULL,
    ceiling_at_approval     REAL    NOT NULL,
    overage_usd             REAL    NOT NULL,
    note                    TEXT,
    expires_at              TEXT    NOT NULL,  -- ISO 8601 UTC; auto-set to approved_at + 24h
    status                  TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','consumed','expired'))
);

CREATE INDEX IF NOT EXISTS idx_finance_approvals_episode ON finance_approvals(episode_id);
CREATE INDEX IF NOT EXISTS idx_finance_approvals_status  ON finance_approvals(status);

-- ---------------------------------------------------------------------------
-- worker_control
-- Key-value store for cross-process worker control signals.
-- Written by dashboard operator; polled by worker between pipeline stages.
--
-- Standard keys:
--   queue_paused    '0' | '1'  — pause new job claiming (worker skips poll)
--   stop_requested  '0' | '1'  — stop worker after current job finishes
--   worker_status   'stopped' | 'idle' | 'claiming' | 'running' | 'cancelling'
--   active_queue_id UUID or ''  — queue_id of the currently executing item
--   active_run_id   UUID or ''  — run_id of the currently executing run
--   worker_id       UUID or ''  — UUID of the current worker process
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS worker_control (
    key        TEXT NOT NULL PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
