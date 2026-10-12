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
                               CHECK(platform IN ('youtube','tiktok','instagram','facebook','linkedin')),
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
    reach              INTEGER,   -- unique accounts reached (Facebook / Instagram; NULL elsewhere)
    follows            INTEGER,   -- follower conversions attributed to this post
    reposts            INTEGER,   -- reshares onto another account's feed (distinct from shares)
    engagement_rate    REAL,      -- platform-computed engagement rate in PERCENT (e.g. 2.66 = 2.66%)
    skip_rate          REAL,      -- Instagram Reels skip rate in PERCENT (early scroll-away); NULL elsewhere
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

-- Account-level / cross-post rollups from Zernio's aggregate analytics
-- endpoints (content-decay, best-time, posting-frequency, follower-stats).
-- These are NOT per-episode: Zernio computes them over the whole connected
-- account, so scope is 'factory' for the engagement/timing rollups and the
-- Cortex channel_id for the follower series (mapped from the Zernio account).
CREATE TABLE IF NOT EXISTS channel_analytics_rollup (
    rollup_id      TEXT NOT NULL PRIMARY KEY,
    scope          TEXT NOT NULL,          -- 'factory' | channel_id
    metric_kind    TEXT NOT NULL
                   CHECK(metric_kind IN ('content_decay','best_time',
                                         'posting_frequency','follower_stats')),
    snapshot_date  TEXT NOT NULL,
    payload_json   TEXT NOT NULL,          -- normalised list/dict for that kind
    source         TEXT NOT NULL DEFAULT 'api'
                   CHECK(source IN ('api','manual','mock')),
    imported_at    TEXT NOT NULL,
    UNIQUE(scope, metric_kind, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_car_scope ON channel_analytics_rollup(scope, metric_kind, snapshot_date DESC);

-- Platform-native ACCOUNT-level metric snapshots (2026-09-06): each
-- platform's own analytics API via Zernio — YouTube channel-insights
-- (views / estimatedMinutesWatched / subs), Instagram account-insights,
-- TikTok account-insights, Facebook page-insights. One row per
-- (channel, platform, day); metrics_json is the platform's own metric bag.
CREATE TABLE IF NOT EXISTS channel_platform_insights (
    insight_id     TEXT NOT NULL PRIMARY KEY,
    channel_id     TEXT NOT NULL,
    platform       TEXT NOT NULL
                   CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    snapshot_date  TEXT NOT NULL,
    metrics_json   TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'api'
                   CHECK(source IN ('api','manual','mock')),
    imported_at    TEXT NOT NULL,
    UNIQUE(channel_id, platform, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_cpi ON channel_platform_insights(channel_id, platform, snapshot_date DESC);

-- Audience demographics per channel × platform (2026-09-06): YouTube +
-- Instagram expose follower age / gender / geography. One row per
-- (channel, platform, dimension, day); breakdown_json is [{label, pct}].
-- A stark, actionable signal — hypogeum's YouTube audience is ~66% aged
-- 55+ and ~79% male, which the content agents should adapt register to.
CREATE TABLE IF NOT EXISTS channel_audience (
    audience_id    TEXT NOT NULL PRIMARY KEY,
    channel_id     TEXT NOT NULL,
    platform       TEXT NOT NULL
                   CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    dimension      TEXT NOT NULL
                   CHECK(dimension IN ('age','gender','city','country')),
    snapshot_date  TEXT NOT NULL,
    breakdown_json TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'api'
                   CHECK(source IN ('api','manual','mock')),
    imported_at    TEXT NOT NULL,
    UNIQUE(channel_id, platform, dimension, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_caud ON channel_audience(channel_id, platform, dimension, snapshot_date DESC);

-- Audience comment text on published posts (2026-09-06), from Zernio's
-- /v1/inbox/comments/{postId}. `is_owner` = the channel's own reply /
-- pinned comment (excluded from the audience-voice signal). Real audience
-- comments are a strong editorial signal once a channel has any volume.
CREATE TABLE IF NOT EXISTS post_comments (
    comment_id       TEXT NOT NULL PRIMARY KEY,   -- the platform's own comment id
    episode_id       TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    platform         TEXT NOT NULL
                     CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    platform_post_id TEXT,
    author           TEXT,
    is_owner         INTEGER NOT NULL DEFAULT 0,
    text             TEXT NOT NULL,
    like_count       INTEGER,
    published_at     TEXT,
    source           TEXT NOT NULL DEFAULT 'api'
                     CHECK(source IN ('api','manual','mock')),
    imported_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pcmt_ep ON post_comments(episode_id, is_owner);
CREATE INDEX IF NOT EXISTS idx_pcmt_ch ON post_comments(channel_id, is_owner, published_at DESC);

-- ---------------------------------------------------------------------------
-- Cortex Advisory (2026-09-06): performance intelligence for content Cortex
-- did NOT produce — external clients whose social accounts the operator
-- manages. A client is a tenant; its published posts are content_items;
-- the analytics/learning stack runs on them exactly as it does on episodes
-- (content_id doubles as the platform_analytics episode_id key). No
-- production, no pipeline — observe + advise.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS clients (
    client_id        TEXT NOT NULL PRIMARY KEY,
    name             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK(status IN ('active','paused','archived')),
    engagement_mode  TEXT NOT NULL DEFAULT 'observe'
                     CHECK(engagement_mode IN ('observe','manage')),
    analysis_depth   TEXT NOT NULL DEFAULT 'metadata'
                     CHECK(analysis_depth IN ('metadata','full_video')),
    timezone         TEXT NOT NULL DEFAULT 'UTC',
    brief_json       TEXT,                 -- who the client is, goals, brand notes
    monthly_analysis_budget_usd REAL NOT NULL DEFAULT 5.0,
    stage            TEXT NOT NULL DEFAULT 'active',   -- 'prospect' (pre-contract, public data only) | 'active'
    managed_since    TEXT,                 -- ISO date the operator took over; before/after split point
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- One connected social account for a client. Points at an existing
-- provider_accounts row (reuses sync_zernio_accounts / the Zernio roster).
CREATE TABLE IF NOT EXISTS managed_accounts (
    managed_account_id  TEXT NOT NULL PRIMARY KEY,
    client_id           TEXT NOT NULL,
    platform            TEXT NOT NULL
                        CHECK(platform IN ('youtube','tiktok','instagram','facebook','linkedin')),
    handle              TEXT,
    provider_id         TEXT NOT NULL DEFAULT 'zernio',
    provider_account_id TEXT,              -- provider_accounts.provider_account_id (Zernio _id)
    provider_profile_id TEXT,              -- Zernio profileId
    native_account_id   TEXT,              -- YouTube channel id, etc.
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(client_id, platform, handle)
);
CREATE INDEX IF NOT EXISTS idx_macct_client ON managed_accounts(client_id, enabled);
CREATE INDEX IF NOT EXISTS idx_macct_provacct ON managed_accounts(provider_account_id);

-- One published post for a client, from any source.
CREATE TABLE IF NOT EXISTS content_items (
    content_id         TEXT NOT NULL PRIMARY KEY,   -- 'ci_' + sha1(platform + ':' + platform_post_id)[:16]
    client_id          TEXT NOT NULL,
    managed_account_id TEXT,
    platform           TEXT NOT NULL
                       CHECK(platform IN ('youtube','tiktok','instagram','facebook','linkedin')),
    platform_post_id   TEXT,
    zernio_post_id     TEXT,
    url                TEXT,
    title              TEXT,
    caption            TEXT,
    thumbnail_url      TEXT,
    media_type         TEXT,                        -- video | image | carousel
    published_at       TEXT,
    source             TEXT NOT NULL DEFAULT 'zernio_external'
                       CHECK(source IN ('zernio_external','zernio_late','manual_url','csv','public','instagram_graph','linkedin_export')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(platform, platform_post_id)
);
CREATE INDEX IF NOT EXISTS idx_citem_client ON content_items(client_id, published_at DESC);

-- The content-analysis agent's read of one content_item (metadata + thumbnail).
CREATE TABLE IF NOT EXISTS content_profiles (
    profile_id      TEXT NOT NULL PRIMARY KEY,
    content_id      TEXT NOT NULL UNIQUE,
    analysis_json   TEXT NOT NULL,
    analysis_depth  TEXT NOT NULL DEFAULT 'metadata'
                    CHECK(analysis_depth IN ('metadata','full_video')),
    model_slug      TEXT,
    cost_usd        REAL,
    created_at      TEXT NOT NULL
);

-- Registry of the raw shot material available for a client (P5). No processing.
CREATE TABLE IF NOT EXISTS client_assets (
    asset_id    TEXT NOT NULL PRIMARY KEY,
    client_id   TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other'
                CHECK(kind IN ('footage','photo','graphic','audio','other')),
    label       TEXT NOT NULL,
    location    TEXT,                       -- Drive folder path / uploaded file ref
    note        TEXT,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casset_client ON client_assets(client_id);

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
-- channel_learning_directives — the executable output of an applied
-- learning_action. Where content_volume nudges production_cadence directly,
-- the advisory dimensions (thumbnail / hook / pacing / topic / ...) each
-- become a short, channel-scoped directive here that the relevant agent
-- reads on the next episode. This is Channel Learning — scoped to one
-- channel, never cross-channel (that is Factory Learning). See
-- hypogeum/channel_learning.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_learning_directives (
    directive_id     TEXT    NOT NULL PRIMARY KEY,
    channel_id       TEXT    NOT NULL,
    dimension        TEXT    NOT NULL,   -- thumbnail|hook|pacing|topic|title|description
    action_type      TEXT    NOT NULL,   -- prefer|avoid|change|test|increase|decrease
    directive_text   TEXT    NOT NULL,   -- the instruction the agent injects
    source_action_id TEXT,
    source_episode   TEXT,
    created_at       TEXT    NOT NULL,
    superseded_at    TEXT,               -- set when a newer directive for the
                                         -- same (channel, dimension) replaces it
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_cld_channel   ON channel_learning_directives(channel_id, active);
CREATE INDEX IF NOT EXISTS idx_cld_dimension ON channel_learning_directives(channel_id, dimension, active);

-- ---------------------------------------------------------------------------
-- retention_moments: what was said and shown at the seconds viewers LEFT (or re-watched).
-- Written by hypogeum/moment_learning.py from the platform retention curve + the episode's own
-- narration/script/visual packages; replaced wholesale per video on every learning cycle.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retention_moments (
    moment_id     TEXT    NOT NULL PRIMARY KEY,   -- <video_id>:<kind>:<n>
    channel_id    TEXT    NOT NULL,
    episode_id    TEXT    NOT NULL,
    video_id      TEXT    NOT NULL,
    platform      TEXT    NOT NULL DEFAULT 'youtube',
    kind          TEXT    NOT NULL,               -- exit | rewatch
    t0            REAL    NOT NULL,
    t1            REAL    NOT NULL,
    loss          REAL    NOT NULL,               -- share of the starting audience (negative = re-watch)
    video_views   INTEGER,
    beat_id       TEXT,
    beat_index    INTEGER,
    beats_total   INTEGER,
    signals_json  TEXT,
    spoken_text   TEXT,
    scene_id      TEXT,
    visual_json   TEXT,                           -- subject / framing / motion_type / mood
    frame_path    TEXT,                           -- the actual frame at that second (when the render is reachable)
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rm_channel ON retention_moments(channel_id, video_id);

-- episode_timelines: the narration/script/scene timeline of an episode, cached the first time its folder was readable,
-- so retention learning keeps working when the archive (NAS) is unreachable (episodes are moved there after release).
-- episode_traits: script-level traits of an episode (the hook pattern it opened with and its first spoken line), cached
-- when the episode folder was readable so hook learning does not depend on the archive (NAS) being reachable.
CREATE TABLE IF NOT EXISTS episode_traits (
    episode_id   TEXT NOT NULL PRIMARY KEY,
    hook_pattern TEXT,
    hook_text    TEXT,
    cached_at    TEXT NOT NULL
);

-- youtube_traffic_insights: where a channel's YouTube views COME FROM and what viewers searched, straight from the
-- YouTube Analytics API (hypogeum/youtube_traffic.py). One snapshot per channel per day, replaced wholesale.
-- dimension: traffic_type | search_term | ext_url | related_video | subscribed | device
--            | video_traffic (key=video, subkey=traffic type) | video_search (key=video, subkey=term)
CREATE TABLE IF NOT EXISTS youtube_traffic_insights (
    channel_id          TEXT NOT NULL,
    snapshot_date       TEXT NOT NULL,
    dimension           TEXT NOT NULL,
    key                 TEXT NOT NULL,
    subkey              TEXT NOT NULL DEFAULT '',
    views               INTEGER,
    minutes_watched     REAL,
    avg_view_duration_s REAL,
    avg_view_pct        REAL,          -- 0-100 as YouTube reports it
    window_days         INTEGER,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (channel_id, snapshot_date, dimension, key, subkey)
);
CREATE INDEX IF NOT EXISTS idx_yti_channel ON youtube_traffic_insights(channel_id, dimension, snapshot_date);

CREATE TABLE IF NOT EXISTS episode_timelines (
    episode_id     TEXT NOT NULL PRIMARY KEY,
    timeline_json  TEXT NOT NULL,
    total_seconds  REAL NOT NULL,
    cached_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- model_registry — the versioned generation-model catalogue Cortex tracks
-- (Phase 6 / Factory Capability Evolution substrate). Synced from a
-- provider's model listing; a model that disappears from a fresh sync is
-- marked 'sunset' (deprecation detected), so Cortex notices when a model a
-- channel depends on goes away or changes price. Factory Learning —
-- cross-channel, never inside a Channel Package. See hypogeum/model_registry.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_registry (
    model_id         TEXT    NOT NULL PRIMARY KEY,   -- provider model slug
    provider         TEXT    NOT NULL DEFAULT 'muapi',
    category         TEXT,                           -- text_to_video|image_to_video|text_to_image|...
    family           TEXT,
    price_usd        REAL,
    price_currency   TEXT    DEFAULT 'USD',
    dynamic_pricing  INTEGER NOT NULL DEFAULT 0,
    lifecycle_status TEXT    NOT NULL DEFAULT 'active'
                             CHECK(lifecycle_status IN ('active','deprecated','sunset','unknown')),
    notes            TEXT,
    first_seen_at    TEXT    NOT NULL,
    last_seen_at     TEXT    NOT NULL,               -- last sync this model was in the catalogue
    last_synced_at   TEXT    NOT NULL,
    last_price_usd   REAL,                           -- previous price, when it changed
    price_changed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mr_category  ON model_registry(category, lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_mr_lifecycle ON model_registry(lifecycle_status);

-- ---------------------------------------------------------------------------
-- provider_evidence — one row per real generation attempt: model, outcome,
-- latency, cost, and (when measurable) a quality proxy. Passive Provider
-- Learning (Phase 6): the evidence the Factory Evolution loop compares
-- candidates against. Factory Learning — cross-channel. See
-- hypogeum/provider_evidence.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_evidence (
    evidence_id    TEXT    NOT NULL PRIMARY KEY,
    provider       TEXT    NOT NULL DEFAULT 'muapi',
    model_id       TEXT    NOT NULL,
    source         TEXT    NOT NULL,   -- 'bakeoff' | 'production' | ...
    ok             INTEGER NOT NULL,
    failure_reason TEXT,
    latency_s      REAL,
    cost_usd       REAL,
    quality_proxy  REAL,               -- e.g. motion score 0-100, or None
    duration_s     REAL,
    channel_id     TEXT,
    episode_id     TEXT,
    recorded_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pe_model ON provider_evidence(model_id, recorded_at);

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
-- One row per episode × platform × deliverable publishing attempt.
-- Written by store.record_publishing_outcome() — never by live publishing code.
-- publishing_status: 'success' | 'failed' | 'pending' | 'skipped' | 'unknown'
--   | 'uploaded_to_inbox' — a terminal-but-not-fully-published state (TikTok
--   Upload-to-Inbox: the draft was delivered to the creator's TikTok inbox,
--   but a human must still complete posting in-app). Added 2026-07-26;
--   existing databases are migrated in place by
--   db.py::_migrate_publishing_outcomes_status_enum().
-- deliverable_id: 'main_episode' | 'short_<short_id>' — distinguishes the
--   flagship upload from individual Shorts/Reels on the same platform.
--   Multi-platform publishing idempotency (2026-07-25): existing databases
--   are migrated in place by db.py::_migrate_publishing_outcomes_deliverable_id()
--   (this CREATE TABLE only governs brand-new databases).
-- UNIQUE(episode_id, platform, deliverable_id) — one canonical outcome per
--   episode/platform/deliverable triple.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publishing_outcomes (
    outcome_id          TEXT NOT NULL PRIMARY KEY,
    episode_id          TEXT NOT NULL,
    run_id              TEXT,
    platform            TEXT NOT NULL,
    deliverable_id      TEXT NOT NULL DEFAULT 'main_episode',
    publishing_status   TEXT NOT NULL DEFAULT 'unknown'
                             CHECK(publishing_status IN
                                 ('success','failed','pending','skipped','unknown','uploaded_to_inbox')),
    platform_video_id   TEXT,
    published_at        TEXT,
    url                 TEXT,
    failure_reason      TEXT,
    recorded_at         TEXT NOT NULL,
    metadata_version    TEXT NOT NULL DEFAULT 'v1',
    UNIQUE(episode_id, platform, deliverable_id)
);

CREATE INDEX IF NOT EXISTS idx_pub_outcomes_episode     ON publishing_outcomes(episode_id);
CREATE INDEX IF NOT EXISTS idx_pub_outcomes_platform    ON publishing_outcomes(platform);
CREATE INDEX IF NOT EXISTS idx_pub_outcomes_status      ON publishing_outcomes(publishing_status);
CREATE INDEX IF NOT EXISTS idx_pub_outcomes_deliverable ON publishing_outcomes(deliverable_id);

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

-- ---------------------------------------------------------------------------
-- factory_settings
-- Key-value store for factory-wide (not channel-scoped) operator settings
-- that a client can change from the dashboard without touching .env.
--
-- Standard keys:
--   archive_root   absolute filesystem path — where published episode assets
--                  are relocated to after publish + catalog. Empty/absent =>
--                  fall back to Config.HYPOGEUM_ARCHIVE_ROOT, then the
--                  built-in default (<project>/.hypogeum-assets/archive).
--                  Resolved by hypogeum.archive_storage.resolve_archive_root().
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS factory_settings (
    key        TEXT NOT NULL PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- bounded_runs / bounded_run_claims
-- Durable, permanent claim allowance for run_worker.py's --max-episodes
-- bounded mode (hypogeum.queue.claim_next_bounded() /
-- hypogeum.db.provision_bounded_run() / hypogeum.db.record_bounded_run_claim()).
--
-- Deliberately NOT derived from run_queue.worker_id: release() clears that
-- column back to NULL on requeue, and a restarted process may not even reuse
-- the same worker_id, so neither is a valid durable consumption record.
-- bounded_run_id is supplied explicitly by the caller (never auto-generated
-- by the claim path) and stays fixed across any restart of the same logical
-- bounded run — that fixed identity is what makes the claim count durable.
--
-- The claim count is never a standalone mutable counter: it is always
-- COUNT(*) FROM bounded_run_claims WHERE bounded_run_id = ? — reconstructable
-- from an append-only ledger, not a summary integer that could drift from
-- reality. A row in bounded_run_claims, once inserted (in the same SQLite
-- transaction as the run_queue claim it corresponds to — see
-- claim_next_bounded()), is never deleted — not when processing later
-- fails, the queue item is released, or the worker crashes/restarts. The
-- allowance, once spent, stays spent.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bounded_runs (
    bounded_run_id      TEXT    NOT NULL PRIMARY KEY,
    max_episode_claims  INTEGER NOT NULL CHECK (max_episode_claims > 0),
    publishing_allowed  INTEGER NOT NULL DEFAULT 0 CHECK (publishing_allowed IN (0, 1)),
    created_at          TEXT    NOT NULL,
    completed_at        TEXT,
    status              TEXT    NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'exhausted', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS bounded_run_claims (
    bounded_run_id TEXT NOT NULL REFERENCES bounded_runs(bounded_run_id),
    queue_id       TEXT NOT NULL,
    episode_id     TEXT NOT NULL,
    claimed_at     TEXT NOT NULL,
    PRIMARY KEY (bounded_run_id, queue_id),
    UNIQUE (bounded_run_id, episode_id)
);

-- ---------------------------------------------------------------------------
-- audit_records
-- Minimal persistence form of DATA_MODEL.md's canonical "Audit Record"
-- entity ("Represents a significant platform event"). Generic across any
-- entity_type/action — not specific to factory policy or budget. Append-only
-- through the public API (hypogeum/audit.py::record_audit_event()); no
-- update or delete functions exist by design.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_records (
    audit_id        TEXT NOT NULL PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    action          TEXT NOT NULL,
    field_name      TEXT,
    previous_value  TEXT,
    new_value       TEXT,
    triggered_by    TEXT NOT NULL DEFAULT 'operator',
    reason          TEXT,
    occurred_at     TEXT NOT NULL,
    metadata_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_entity
    ON audit_records(entity_type, entity_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_occurred_at
    ON audit_records(occurred_at DESC);

-- ---------------------------------------------------------------------------
-- runtime_leases
-- Generic Factory Core fencing-token lease table (docs/architecture/
-- AUTHORIZED_GENERATION_ENVELOPE.md Phase 5 gate check 19; implementation
-- blueprint Commit 2 / Part 3). Protects exactly one resource_id (an
-- envelope_id or authorization_id) per row: only the current owner_id
-- holding the current fencing_token may append protected write-ahead
-- attempt evidence for that resource (hypogeum/provider_receipt.py calls
-- hypogeum/db.py::validate_lease() before every append). A stale owner's
-- fencing_token no longer matches after takeover, so it fails closed
-- automatically -- see hypogeum/db.py::acquire_lease()/renew_lease()/
-- release_lease() for the exact atomic-conditional-UPDATE mechanics
-- (same pattern as run_queue's claim_next_queue_item()). Rows are never
-- deleted: a released or superseded lease stays as its own audit trail.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runtime_leases (
    resource_id     TEXT NOT NULL PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    fencing_token   INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                         CHECK(status IN ('active','released','expired')),
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    released_at     TEXT,
    metadata_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_runtime_leases_status
    ON runtime_leases(status);

CREATE INDEX IF NOT EXISTS idx_runtime_leases_expires_at
    ON runtime_leases(expires_at);

-- ---------------------------------------------------------------------------
-- budget_reservations
-- Generic Factory Core budget ledger (docs/architecture/
-- AUTHORIZED_GENERATION_ENVELOPE.md Phase 7; implementation blueprint
-- Commit 3 / Part 2). One row per (scope_kind, scope_ref, idempotency_key)
-- reservation. Exact monetary values stored as TEXT (Decimal string
-- representation), never a SQLite REAL/float column -- see
-- hypogeum/budget_scopes.py for the Decimal round-trip. Reservation is a
-- check-then-insert performed inside one BEGIN IMMEDIATE transaction
-- (hypogeum/db.py::reserve_budget()), the same atomicity technique
-- runtime_leases's conditional-UPDATE pattern provides for leases --
-- BEGIN IMMEDIATE acquires SQLite's write lock for the whole transaction,
-- so two concurrent workers can never both observe room under the
-- ceiling and both insert; the loser's transaction is serialized after
-- the winner's and sees the updated sum. The UNIQUE constraint makes a
-- repeated reservation attempt for the same identity an idempotent
-- replay (returns the existing row) rather than a duplicate reservation.
-- Rows are never deleted: 'released' and 'committed' are terminal
-- statuses that remain as their own audit trail.
--
-- Phase 3 (per-channel weekly budget enforcement, 2026-08-25): scope_kind
-- gains 'channel_weekly', whose scope_ref is "<channel_id>:<ISO week key>"
-- (e.g. "ch_x:2026-W35"). The five weekly_* columns are NULL for every
-- other scope kind and carry the immutable, at-reservation-time snapshot
-- of the window identity a CHANNEL_WEEKLY row was authorized against
-- (channel attribution, channel timezone snapshot, resolved UTC window
-- boundaries, ceiling snapshot). They are written inside the same
-- BEGIN IMMEDIATE insert as the reservation itself -- never patched in
-- afterward -- so an audit row always proves exactly which window policy
-- the reservation consumed, and an in-week channel timezone change can be
-- detected against persisted evidence instead of recomputed history.
-- Existing databases are widened to this exact shape by the transactional
-- table rebuild in hypogeum/db.py::_migrate_budget_reservations_scope_check().
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id    TEXT NOT NULL PRIMARY KEY,
    scope_kind        TEXT NOT NULL
                           CHECK(scope_kind IN ('call','run','episode','organization','retry','fallback','channel_weekly')),
    scope_ref         TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL,
    reserved_amount   TEXT NOT NULL,
    currency          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'reserved'
                           CHECK(status IN ('reserved','committed','released')),
    committed_amount  TEXT,
    created_at        TEXT NOT NULL,
    resolved_at       TEXT,
    channel_id        TEXT,
    weekly_timezone_snapshot   TEXT,
    weekly_window_start_utc    TEXT,
    weekly_window_end_utc      TEXT,
    weekly_ceiling_snapshot    TEXT,
    UNIQUE(scope_kind, scope_ref, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_scope
    ON budget_reservations(scope_kind, scope_ref, status);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_channel_weekly_active
    ON budget_reservations(channel_id, status)
    WHERE scope_kind = 'channel_weekly';

-- ---------------------------------------------------------------------------
-- circuit_breakers
-- Generic Factory Core circuit-breaker evidence table (architecture
-- Phase 7.2; blueprint Commit 3 / Part 3). One row per breaker_id (a
-- caller-defined composite key, e.g. "max_provider_calls:run:run_1").
-- tripped is monotonic: once 1, a later record_breaker_evidence() call
-- can update observed_value/evidence but can never silently flip it back
-- to 0 -- only hypogeum/db.py::reset_breaker() (a distinct, explicit
-- function; never called automatically by this schema or by
-- record_breaker_evidence()) may clear it, matching the architecture's
-- "reset must never happen automatically" rule. Surviving as a normal
-- table row is what lets breaker state be reconstructed identically
-- after a process restart, rather than depending on in-memory state.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS circuit_breakers (
    breaker_id              TEXT NOT NULL PRIMARY KEY,
    breaker_type            TEXT NOT NULL,
    scope_kind              TEXT NOT NULL,
    scope_ref               TEXT NOT NULL,
    observed_value          TEXT,
    configured_limit        TEXT,
    tripped                 INTEGER NOT NULL DEFAULT 0,
    triggering_attempt_id   TEXT,
    tripped_at              TEXT,
    reset_at                TEXT,
    reset_by                TEXT,
    evidence_json           TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_circuit_breakers_tripped
    ON circuit_breakers(tripped);

-- ---------------------------------------------------------------------------
-- publication_intents / publishing_attempts / publishing_receipts
--
-- Durable write-ahead publishing submission boundary (ADR-013 Decision 7,
-- item 2 — DECISION_LOG.md). Provider-neutral: never references a specific
-- provider by name in this schema (platform stays youtube/tiktok/instagram/
-- facebook; provider_id is a free-text column populated by the caller, e.g.
-- 'youtube_direct' today, other adapters later — see ADR-013 Decision 1's
-- provider-vs-platform distinction).
--
-- Three durable identities, deliberately separate (never conflated):
--   publication_intents  -- one deliberate publish/republish operation.
--                            Retries of the SAME intent reuse its id; a
--                            deliberate republish (e.g. after the external
--                            publication was deleted) requires a NEW,
--                            explicitly-provisioned intent -- never minted
--                            automatically by a retry.
--   publishing_attempts  -- one external submission attempt within an
--                            intent. Committed with state='prepared'
--                            BEFORE any external call is made -- see
--                            hypogeum.publishing.submission_ledger, the
--                            sole writer of this table.
--   publishing_receipts  -- the acknowledged external result for one
--                            attempt, once known. Never stores credentials
--                            or authorization headers.
--
-- This mirrors the proven write-ahead-persistence / immutable-identity /
-- fail-closed-transition concepts already used for paid generation calls
-- (hypogeum.provider_receipt.WriteAheadReceiptStore,
-- hypogeum.authorization_store) without reusing those classes/tables
-- directly -- ownership differs (publishing is already DB-table-backed via
-- publishing_outcomes, unlike the file-backed generation receipt trail).
--
-- publishing_outcomes (existing, unchanged by this migration) remains the
-- canonical read model for "is this deliverable published" consulted by
-- the rest of the pipeline (get_publishing_outcome/record_publishing_outcome);
-- these three new tables are the write-ahead safety boundary sitting in
-- front of it, not a replacement for it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS publication_intents (
    publication_intent_id TEXT NOT NULL PRIMARY KEY,
    channel_id             TEXT,
    episode_id             TEXT NOT NULL,
    deliverable_id         TEXT NOT NULL,
    platform               TEXT NOT NULL
                                CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    status                 TEXT NOT NULL DEFAULT 'open'
                                CHECK(status IN ('open','succeeded','failed_closed','cancelled')),
    created_at              TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

-- At most one OPEN intent per (episode_id, platform, deliverable_id) --
-- enforced atomically at the DB level (SQLite partial unique index), not
-- just in application code, so a race between two callers can never both
-- succeed at opening a second one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_intents_one_open
    ON publication_intents(episode_id, platform, deliverable_id)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_publication_intents_lookup
    ON publication_intents(episode_id, platform, deliverable_id);

CREATE TABLE IF NOT EXISTS publishing_attempts (
    attempt_id             TEXT NOT NULL PRIMARY KEY,
    publication_intent_id  TEXT NOT NULL REFERENCES publication_intents(publication_intent_id),
    channel_id              TEXT,
    episode_id               TEXT NOT NULL,
    deliverable_id            TEXT NOT NULL,
    platform                  TEXT NOT NULL
                                   CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    provider_id                TEXT NOT NULL,
    provider_request_key        TEXT,
    attempt_number                INTEGER NOT NULL DEFAULT 1,
    state                          TEXT NOT NULL DEFAULT 'prepared'
                                        CHECK(state IN (
                                            'prepared', 'submitting', 'acknowledged', 'succeeded',
                                            'failed_definitive', 'ambiguous', 'reconciliation_required',
                                            'cancelled'
                                        )),
    last_error_category              TEXT,
    last_error_message                 TEXT,
    created_at                          TEXT NOT NULL,
    updated_at                           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publishing_attempts_intent
    ON publishing_attempts(publication_intent_id);

CREATE INDEX IF NOT EXISTS idx_publishing_attempts_state
    ON publishing_attempts(state);

CREATE TABLE IF NOT EXISTS publishing_receipts (
    receipt_id                  TEXT NOT NULL PRIMARY KEY,
    attempt_id                   TEXT NOT NULL REFERENCES publishing_attempts(attempt_id),
    provider_id                    TEXT NOT NULL,
    platform                        TEXT NOT NULL
                                         CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    provider_publication_id           TEXT,
    native_platform_content_id          TEXT,
    provider_status                       TEXT,
    acknowledged_at                        TEXT NOT NULL,
    raw_receipt_metadata_json                TEXT,
    UNIQUE(attempt_id)
);

-- ---------------------------------------------------------------------------
-- provider_profiles / provider_accounts / channel_provider_bindings /
-- provider_content_mappings
--
-- Provider/account/profile/content OPERATIONAL mappings (ADR-013 Decision 7,
-- item 3 -- DECISION_LOG.md). Owned by the operational database per Decision
-- 3's boundary: external provider profile/account IDs, native platform
-- account IDs, connection/reconnection status, provider/native content IDs,
-- and their relationships to Cortex channels, publication_intents,
-- publishing_attempts, and publishing_receipts. Provider-neutral -- never
-- references a specific provider by name; provider_id is a free-text column
-- populated by the caller (e.g. 'youtube_direct' today, other adapters
-- later -- see ADR-013 Decision 1's provider-vs-platform distinction).
--
-- Explicitly NOT this section's job (kept out per Decision 3):
--   - credential material / credential metadata -- secret_profiles owns
--     that exclusively; secret_profiles.channel_id scopes a *credential*,
--     never an account or profile identity.
--   - logical channel configuration (enabled platforms, provider
--     preference, publishing policy) -- owned by the Channel Package
--     (hypogeum.channel_config.PublishingProfile); this section only
--     records what a preference *resolved to operationally*, not the
--     preference itself.
--
-- provider_profiles  -- one external provider-side profile/workspace
--                        (e.g. a multi-platform provider's tenant/workspace
--                        spanning several platform accounts).
--                        UNIQUE(provider_id, external_profile_id) is the
--                        provider/external-profile identity.
-- provider_accounts   -- one external provider-side account for one
--                        destination platform. provider_profile_id is
--                        nullable (a provider need not always group
--                        accounts under a profile). One profile can own
--                        several platform accounts (one per platform, or
--                        more). native_platform_account_id is distinct
--                        from external_account_id -- the former is the
--                        destination platform's own identity (e.g.
--                        YouTube channel ID), the latter is the
--                        provider's own internal identity for that account
--                        -- never conflated.
-- channel_provider_bindings -- the operational binding between one Cortex
--                        channel and provider resources, for one
--                        capability (publishing|analytics) and one
--                        destination platform. A partial unique index
--                        enforces at most one ENABLED binding per
--                        (channel_id, capability, platform, logical_alias)
--                        -- no automatic ordered-fallback is implemented in
--                        this slice (none is defined by the architecture
--                        yet); a conflicting bind attempt must be rejected
--                        by the repository layer, not silently allowed to
--                        race the unique index.
-- provider_content_mappings -- links one published deliverable to its
--                        provider-side and native-platform-side content
--                        identity, plus the exact publication_intent /
--                        publishing_attempt / publishing_receipt that
--                        produced it. Deliberately NOT keyed by
--                        (episode_id, platform, deliverable_id) alone --
--                        a deliberate republish (new publication_intent_id)
--                        produces a NEW mapping row; prior mapping rows for
--                        the same episode/platform/deliverable are never
--                        overwritten or deleted, preserving history for
--                        audit. UNIQUE(receipt_id) ties at most one
--                        mapping to the receipt that created it (NULLs are
--                        distinct in SQLite, so mappings not tied to a
--                        receipt are unaffected).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS provider_profiles (
    provider_profile_id     TEXT NOT NULL PRIMARY KEY,
    provider_id               TEXT NOT NULL,
    external_profile_id         TEXT NOT NULL,
    display_label                 TEXT,
    status                          TEXT NOT NULL DEFAULT 'active'
                                         CHECK(status IN ('active','disconnected','revoked','needs_reconnection')),
    metadata_json                     TEXT,
    created_at                          TEXT NOT NULL,
    updated_at                            TEXT NOT NULL,
    last_verified_at                       TEXT,
    UNIQUE(provider_id, external_profile_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_profiles_provider
    ON provider_profiles(provider_id);

CREATE TABLE IF NOT EXISTS provider_accounts (
    provider_account_id       TEXT NOT NULL PRIMARY KEY,
    provider_profile_id         TEXT REFERENCES provider_profiles(provider_profile_id),
    provider_id                   TEXT NOT NULL,
    platform                        TEXT NOT NULL
                                         CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    external_account_id               TEXT NOT NULL,
    native_platform_account_id          TEXT,
    display_label                         TEXT,
    status                                  TEXT NOT NULL DEFAULT 'active'
                                                 CHECK(status IN ('active','disconnected','revoked','needs_reconnection')),
    needs_reconnection                        INTEGER NOT NULL DEFAULT 0,
    metadata_json                               TEXT,
    created_at                                    TEXT NOT NULL,
    updated_at                                      TEXT NOT NULL,
    last_verified_at                                  TEXT,
    UNIQUE(provider_id, platform, external_account_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_accounts_profile
    ON provider_accounts(provider_profile_id);
CREATE INDEX IF NOT EXISTS idx_provider_accounts_provider_platform
    ON provider_accounts(provider_id, platform);

CREATE TABLE IF NOT EXISTS channel_provider_bindings (
    binding_id            TEXT NOT NULL PRIMARY KEY,
    channel_id              TEXT NOT NULL,
    capability                 TEXT NOT NULL CHECK(capability IN ('publishing','analytics')),
    platform                     TEXT NOT NULL
                                      CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    provider_id                    TEXT NOT NULL,
    provider_profile_id              TEXT REFERENCES provider_profiles(provider_profile_id),
    provider_account_id                TEXT REFERENCES provider_accounts(provider_account_id),
    logical_alias                        TEXT NOT NULL DEFAULT 'default',
    enabled                                 INTEGER NOT NULL DEFAULT 1,
    created_at                                TEXT NOT NULL,
    updated_at                                  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_provider_bindings_one_active
    ON channel_provider_bindings(channel_id, capability, platform, logical_alias)
    WHERE enabled = 1;

CREATE INDEX IF NOT EXISTS idx_channel_provider_bindings_lookup
    ON channel_provider_bindings(channel_id, capability, platform);

CREATE TABLE IF NOT EXISTS provider_content_mappings (
    mapping_id               TEXT NOT NULL PRIMARY KEY,
    provider_id                 TEXT NOT NULL,
    platform                      TEXT NOT NULL
                                       CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
    provider_profile_id             TEXT REFERENCES provider_profiles(provider_profile_id),
    provider_account_id               TEXT REFERENCES provider_accounts(provider_account_id),
    provider_content_id                 TEXT,
    native_platform_content_id            TEXT,
    channel_id                              TEXT,
    episode_id                                TEXT NOT NULL,
    deliverable_id                              TEXT NOT NULL,
    publication_intent_id                         TEXT REFERENCES publication_intents(publication_intent_id),
    attempt_id                                      TEXT REFERENCES publishing_attempts(attempt_id),
    receipt_id                                        TEXT REFERENCES publishing_receipts(receipt_id),
    status                                              TEXT NOT NULL DEFAULT 'confirmed'
                                                             CHECK(status IN ('confirmed','superseded','removed_externally')),
    created_at                                              TEXT NOT NULL,
    updated_at                                                TEXT NOT NULL,
    last_verified_at                                            TEXT,
    UNIQUE(receipt_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_content_mappings_episode
    ON provider_content_mappings(episode_id, platform, deliverable_id);
CREATE INDEX IF NOT EXISTS idx_provider_content_mappings_intent
    ON provider_content_mappings(publication_intent_id);
CREATE INDEX IF NOT EXISTS idx_provider_content_mappings_attempt
    ON provider_content_mappings(attempt_id);

-- ---------------------------------------------------------------------------
-- media_staging_attempts
--
-- Generic, provider-neutral durable media-staging boundary (blocker
-- identified in docs/operations/audits/ZERNIO_OPERATIONAL_ENABLEMENT_READINESS_2026-08-21.md
-- §4/§7 -- "no working public-media-URL path exists"). Mirrors
-- publishing_attempts' write-ahead pattern exactly (hypogeum.publishing.
-- submission_ledger, ADR-013 Decision 7 item 2): a durable row is
-- committed BEFORE either external boundary (presign, then upload) is
-- called, so a caller retrying after a crash/timeout never blindly
-- re-uploads. provider_id is a free-text column (e.g. 'zernio' today);
-- this table and hypogeum.publishing.media_staging (its sole reader/
-- writer) contain no provider-specific request/response field names --
-- see that module's own docstring.
--
-- Identity for reuse/dedup purposes is (provider_id, channel_id,
-- episode_id, deliverable_id, source_sha256) -- looked up via
-- idx_media_staging_attempts_scope, not a UNIQUE constraint, matching
-- publishing_attempts' own "most recent row for this scope" pattern
-- rather than publication_intents' partial-unique-index pattern (there is
-- no "deliberate re-stage" concept analogous to a deliberate republish
-- here -- a fresh attempt is warranted only after failed_definitive or
-- expired, both structural, never an operator decision).
--
-- Never stores the presigned upload URL (short-lived, sensitive query
-- string) -- only the resulting durable public_url, and only once a
-- provider's own contract confirms that field is the intended, stable
-- media reference (see hypogeum.publishing.zernio_media_staging).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS media_staging_attempts (
    staging_attempt_id      TEXT NOT NULL PRIMARY KEY,
    provider_id                TEXT NOT NULL,
    channel_id                   TEXT,
    episode_id                     TEXT NOT NULL,
    deliverable_id                   TEXT NOT NULL,
    source_sha256                      TEXT NOT NULL,
    source_size_bytes                    INTEGER NOT NULL,
    source_mime_type                       TEXT NOT NULL,
    attempt_number                           INTEGER NOT NULL DEFAULT 1,
    state                                      TEXT NOT NULL DEFAULT 'prepared'
                                                    CHECK(state IN (
                                                        'prepared', 'presigning', 'upload_ready', 'uploading',
                                                        'uploaded', 'failed_definitive', 'failed_ambiguous',
                                                        'expired', 'reconciliation_required'
                                                    )),
    public_url                                     TEXT,
    remote_object_id                                 TEXT,
    expires_at                                         TEXT,
    last_error_category                                  TEXT,
    last_error_message                                     TEXT,
    metadata_json                                            TEXT,
    created_at                                                 TEXT NOT NULL,
    updated_at                                                   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_staging_attempts_scope
    ON media_staging_attempts(provider_id, channel_id, episode_id, deliverable_id, source_sha256);
CREATE INDEX IF NOT EXISTS idx_media_staging_attempts_state
    ON media_staging_attempts(state);


-- ---------------------------------------------------------------------------
-- channel_keyword_research (2026-09-03)
-- Per-channel discovery/SEO intelligence: competitor hashtags, high-volume
-- category search terms, trending queries. Researched via Perplexity (see
-- hypogeum/channel_keyword_research.py), cached here, refreshed periodically
-- or on demand. Consumed by the metadata agents so a channel with no name
-- recognition is found through the terms people actually search.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_keyword_research (
    channel_id       TEXT PRIMARY KEY,
    research_json    TEXT NOT NULL,
    researched_at    TEXT NOT NULL,
    source_query_count INTEGER NOT NULL DEFAULT 0,
    total_cost_usd   REAL NOT NULL DEFAULT 0.0,
    metadata_version TEXT NOT NULL DEFAULT 'v1'
);

-- ---------------------------------------------------------------------------
-- channel_publish_slots (2026-09-03)
-- Write-through ledger of the daily publish slot each finished-but-not-yet-
-- live episode has been assigned, so a produce-ahead buffer (several
-- episodes finished, waiting) hands each one a DISTINCT future slot instead
-- of every release-flow compilation independently resolving "today/tomorrow
-- at time_of_day" and colliding. Consumed by
-- hypogeum.publishing.publish_slot_ledger.next_free_now(). Rows are advisory
-- — the authoritative schedule is on the provider (Zernio scheduledFor);
-- this only spaces out assignment. Safe to prune once slot_utc is in the past.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_publish_slots (
    channel_id   TEXT NOT NULL,
    slot_utc     TEXT NOT NULL,
    episode_id   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (channel_id, slot_utc)
);
CREATE INDEX IF NOT EXISTS idx_channel_publish_slots_ch ON channel_publish_slots(channel_id, slot_utc);

-- ---------------------------------------------------------------------------
-- content_grid_cache (2026-09-12)
-- Local cache of the per-post view hypogeum.dashboard_content_grid renders
-- on /channel/<id>/grid -- an in-dashboard "Zernio grid": poster, per-
-- platform status/date, for every post a channel has staged or published.
-- Zernio's own GET /posts/{id} is the source of truth; a live fetch on every
-- page load for a channel with dozens of posts is slow and rate-limit-risky,
-- so each post's last-fetched shape is cached here with a timestamp and
-- refreshed only when stale (see get_content_grid()'s TTL) or on an explicit
-- operator refresh. Rows are disposable -- safe to drop/rebuild any time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS content_grid_cache (
    post_id        TEXT NOT NULL PRIMARY KEY,
    channel_id     TEXT NOT NULL,
    episode_id     TEXT NOT NULL,
    deliverable_id TEXT NOT NULL,
    title          TEXT,
    status         TEXT,
    scheduled_for  TEXT,
    published_at   TEXT,
    thumbnail_url  TEXT,
    thumb_is_landscape INTEGER,  -- 0/1/NULL(unknown) -- a handful of pre-vertical-pivot
                                  -- posts (see docstring) still carry a 16:9 cover; the
                                  -- grid renders those in a 16:9 box instead of forcing
                                  -- them into the 9:16 one and letterboxing to near-invisible
    platforms_json TEXT NOT NULL DEFAULT '[]',
    fetch_error    TEXT,
    cached_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_grid_cache_ch ON content_grid_cache(channel_id, scheduled_for);

-- ---------------------------------------------------------------------------
-- youtube_cover_confirmations (2026-09-29)
-- YouTube only accepts a custom Shorts thumbnail from Studio on a computer --
-- Zernio's API cover never reaches the Shorts surfaces ("Custom thumbnails
-- work on videos only, not Shorts", docs.zernio.com/platforms/youtube). Every
-- published YouTube Short therefore needs a human to upload its designed cover
-- in Studio; the Content Grid flags each one until the operator presses "Done"
-- on its card, which writes a row here. Zernio's own record can never reflect
-- that native edit, so this operator acknowledgement is the only signal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS youtube_cover_confirmations (
    post_id       TEXT NOT NULL PRIMARY KEY,  -- Zernio post id
    channel_id    TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- grid_live_confirmations (2026-10-05)
-- Zernio sometimes marks a platform "failed" after a timeout although the
-- post DID go live ("may have been published externally" -- Pip the Tiny
-- Star on Facebook, 2026-09-25). Retrying would post it twice, and Zernio's
-- record never learns the truth, so the operator checks the platform and
-- presses "It's live" on the Content Grid card; that writes a row here and
-- the card stops asking for attention for that platform.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grid_live_confirmations (
    post_id       TEXT NOT NULL,              -- Zernio post id
    platform      TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL,
    PRIMARY KEY (post_id, platform)
);

-- ---------------------------------------------------------------------------
-- youtube_cover_holds (2026-09-29)
-- Operator rule: no YouTube Short goes public without its designed cover.
-- Since that cover can only be set in Studio on an already-uploaded video,
-- hypogeum.publishing.youtube_cover_hold moves each scheduled post's YouTube
-- entry to upload PRIVATE ~24h before `release_at` (the original slot; the
-- other platforms keep it), and flips it public at release_at only once the
-- cover is confirmed (youtube_cover_confirmations). Unconfirmed at release_at
-- -> status 'late', stays private.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS youtube_cover_holds (
    post_id        TEXT NOT NULL PRIMARY KEY,  -- Zernio post id
    channel_id     TEXT NOT NULL,
    episode_id     TEXT,
    release_at     TEXT NOT NULL,              -- original YouTube slot (UTC ISO)
    upload_at      TEXT NOT NULL,              -- moved YouTube scheduledFor (private upload)
    status         TEXT NOT NULL,              -- held | late | released | release_failed
    held_at        TEXT NOT NULL,
    released_at    TEXT,
    last_error     TEXT
);

-- Personal-branding plan (2026-10-03): LinkedIn personal profiles have no
-- analytics API, so the account-level picture comes from LinkedIn's own
-- creator-analytics Export (.xlsx) — see hypogeum.publishing.linkedin_export.
-- One row per client per export period; per-post numbers go to
-- content_items/platform_analytics like every other platform.
CREATE TABLE IF NOT EXISTS client_linkedin_snapshots (
    snapshot_id                 TEXT NOT NULL PRIMARY KEY,
    client_id                   TEXT NOT NULL,
    period_start                TEXT NOT NULL,
    period_end                  TEXT NOT NULL,
    impressions                 INTEGER,
    members_reached             INTEGER,
    followers_total             INTEGER,
    daily_json                  TEXT NOT NULL DEFAULT '[]',
    audience_demographics_json  TEXT NOT NULL DEFAULT '[]',
    content_demographics_json   TEXT NOT NULL DEFAULT '[]',
    source_file                 TEXT,
    imported_at                 TEXT NOT NULL,
    UNIQUE(client_id, period_end)
);
CREATE INDEX IF NOT EXISTS idx_cli_li_snap ON client_linkedin_snapshots(client_id, period_end DESC);
