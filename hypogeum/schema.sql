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
