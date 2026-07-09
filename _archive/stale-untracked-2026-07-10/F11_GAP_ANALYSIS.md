# F11 Gap Analysis — Intelligence Layer Schema Readiness

**Date:** 2026-06-19  
**Phase:** F11 Prep  
**Scope:** Database schema gaps between current state (F9) and the five F11 intelligence domains  
**Source of truth:** `hypogeum/schema.sql`, `hypogeum/db.py`, `docs/analytics-layer-design.md`, `docs/learning-intelligence-layer.md`, `docs/channel-profile-system.md`

---

## Executive Summary

The current schema (v1 + v2) supports production operations, cost tracking, gate verdicts, and basic analytics ingestion. It is **not ready** for F11 intelligence layers. The five intelligence domains require:

| Domain | Gap Severity | Tables Affected | New Tables Required |
|---|---|---|---|
| Episode Intelligence | **HIGH** | `platform_analytics` | 0 (columns only) |
| Thumbnail Intelligence | **HIGH** | `thumbnail_candidates` | 1 (`thumbnail_performance`) |
| Title Intelligence | **MEDIUM** | `titles` | 0 (columns only) |
| Campaign Intelligence | **CRITICAL** | `episodes`, `episode_topics` | 2 (`campaigns`, `campaign_episodes`) |
| Revenue Intelligence | **HIGH** | `platform_analytics`, `channel_profile_settings` | 0 (columns only) |

**Total migration scope:** 5 table alterations, 2 new tables, ~36 new columns, 3 new views.

---

## Current Schema Inventory

### Tables that feed F11 intelligence

| Table | Current Purpose | F11 Relevance |
|---|---|---|
| `platform_analytics` | Basic metric storage (views, CTR, watch time) | Primary source for all 5 domains |
| `analytics_insights` | Rule-fired derived signals | Episode Intelligence — partial |
| `thumbnail_candidates` | Pre-publish candidates with rank | Thumbnail Intelligence — no outcomes |
| `titles` | Pre-publish title candidates | Title Intelligence — no outcomes |
| `episodes` | Episode registry | Episode + Campaign Intelligence |
| `learning_records` | Generic learning signal store | Learning Intelligence — generic only |
| `signal_windows` | YouTube-specific time-series | Episode Intelligence — partial |
| `channel_profile_settings` | Minimal channel config (F9 table) | Revenue Intelligence — goals absent |
| `archive_index` | Asset archive with `performance_signal` REAL | Thumbnail Intelligence — scalar only |

### What the schema does NOT have

- No retention curve storage (only scalar `avg_view_pct`)
- No derived performance bands (`outperforming`, `above_average`, etc.)
- No post-publish CTR outcome linked back to `thumbnail_candidates` or `titles`
- No campaign entity or campaign_id grouping on episodes
- No `revenue_usd`, `cpm`, or `rpm` columns anywhere
- No monetization goal fields on `channel_profile_settings`
- No stabilization window tracking (`status: pending_stabilization`)
- No `channel_id` on `platform_analytics`

---

## 1. Episode Intelligence

### Current State

`platform_analytics` stores: `views`, `impressions`, `ctr`, `avg_view_pct`, `avg_watch_time_s`, `total_watch_time_s`, `likes`, `comments`, `shares`, `saves`, `platform_video_id`, `raw_json`.

`signal_windows` stores a YouTube-specific time-series for `views`, `watch_minutes`, `avg_view_duration_s`, `likes`, `comments`, `shares`, `subscribers_gained`, `ctr`, `impressions`.

`analytics_insights` fires rules (weak_thumbnail, high_retention, hook_failure) and stores `signal_strength` per `affected_dim`.

### Required State

Per `docs/analytics-layer-design.md` §4.1 and §6.1, `EpisodeAnalytics` requires:

**Core metrics (partially present):**
- `retention_curve` — 10-point float array at 10% intervals (not stored anywhere)
- `subscriber_gain` — not on `platform_analytics`
- `revenue_usd` — not on `platform_analytics`
- `traffic_sources` — not stored (JSON breakdown: search, browse, direct, algorithm)

**Derived metrics (entirely absent):**
- `retention_at_30s` — float, retention at the 30-second mark
- `retention_at_midpoint` — float, retention at 50% of episode duration
- `completion_rate` — float, % viewers who watched ≥80%
- `engagement_rate` — (likes + comments + shares) / views
- `relative_ctr` — this episode's CTR vs. channel 30-day average
- `relative_retention` — this episode's avg_view_pct vs. channel 30-day average
- `performance_band_ctr` — `outperforming | above_average | on_baseline | below_average | underperforming`
- `performance_band_retention` — same enum

**Ingestion lifecycle tracking (entirely absent):**
- `analytics_status` — `pending_stabilization | active | final | ingestion_failed`
- `stabilization_at` — timestamp when stabilization window was met
- `all_platforms_ingested` — INTEGER 0/1
- `platforms_pending` — TEXT JSON array
- `analytics_period_start` — ISO 8601 TEXT
- `analytics_period_end` — ISO 8601 TEXT

**Context (absent):**
- `channel_id` — not on `platform_analytics`; required to compute channel baselines
- `anomalies_json` — TEXT JSON array of fired anomaly_type values for this episode

### Migration Required

**ALTER TABLE `platform_analytics` ADD COLUMN** (12 columns):

```sql
ALTER TABLE platform_analytics ADD COLUMN channel_id            TEXT;
ALTER TABLE platform_analytics ADD COLUMN subscriber_gain       INTEGER;
ALTER TABLE platform_analytics ADD COLUMN revenue_usd           REAL;
ALTER TABLE platform_analytics ADD COLUMN traffic_sources       TEXT;   -- JSON map
ALTER TABLE platform_analytics ADD COLUMN retention_curve       TEXT;   -- JSON float[10]
ALTER TABLE platform_analytics ADD COLUMN retention_at_30s      REAL;
ALTER TABLE platform_analytics ADD COLUMN retention_at_midpoint REAL;
ALTER TABLE platform_analytics ADD COLUMN completion_rate       REAL;
ALTER TABLE platform_analytics ADD COLUMN engagement_rate       REAL;
ALTER TABLE platform_analytics ADD COLUMN relative_ctr          REAL;
ALTER TABLE platform_analytics ADD COLUMN relative_retention    REAL;
ALTER TABLE platform_analytics ADD COLUMN performance_band_ctr  TEXT;
ALTER TABLE platform_analytics ADD COLUMN performance_band_retention TEXT;
ALTER TABLE platform_analytics ADD COLUMN analytics_status      TEXT NOT NULL DEFAULT 'active';
ALTER TABLE platform_analytics ADD COLUMN stabilization_at      TEXT;
ALTER TABLE platform_analytics ADD COLUMN all_platforms_ingested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE platform_analytics ADD COLUMN platforms_pending      TEXT;   -- JSON array
ALTER TABLE platform_analytics ADD COLUMN analytics_period_start TEXT;
ALTER TABLE platform_analytics ADD COLUMN analytics_period_end  TEXT;
ALTER TABLE platform_analytics ADD COLUMN anomalies_json         TEXT;   -- JSON array
```

**New index:**
```sql
CREATE INDEX IF NOT EXISTS idx_pa_channel    ON platform_analytics(channel_id);
CREATE INDEX IF NOT EXISTS idx_pa_status     ON platform_analytics(analytics_status);
```

**New view:**
```sql
CREATE VIEW IF NOT EXISTS v_episode_intelligence AS
SELECT
    pa.record_id,
    pa.episode_id,
    pa.channel_id,
    e.title                         AS episode_title,
    e.topic                         AS episode_topic,
    e.published_at                  AS episode_published_at,
    pa.platform,
    pa.analytics_status,
    pa.views,
    pa.ctr,
    pa.avg_view_pct,
    pa.retention_at_30s,
    pa.completion_rate,
    pa.engagement_rate,
    pa.relative_ctr,
    pa.performance_band_ctr,
    pa.performance_band_retention,
    pa.revenue_usd,
    pa.analytics_period_start,
    pa.analytics_period_end,
    pa.snapshot_date
FROM platform_analytics pa
LEFT JOIN episodes e ON e.episode_id = pa.episode_id;
```

**Add to `_V2_COLUMN_MIGRATIONS` in `db.py`** under `"platform_analytics"` key.

### Dashboard Impact

- **Home KPI strip** — cannot show `performance_band` status badges or goal tracking without `relative_ctr` and `performance_band_ctr`
- **Production page** — episode history cannot show post-publish performance band
- **Channel analytics tab** — CTR trend requires `relative_ctr`; retention trend requires `completion_rate`
- **Analytics insights** — `analytics_insights` rules will fire against stale/stabilizing data without `analytics_status` filter
- **Learning Module** — signals submitted before `all_platforms_ingested = 1` are invalid; filter requires this column

---

## 2. Thumbnail Intelligence

### Current State

`thumbnail_candidates` stores: `candidate_id`, `run_id`, `episode_id`, `rank`, `text_overlay`, `placement`, `visual_prompt`, `selected`.

`archive_index` has a scalar `performance_signal REAL` but no CTR linkage.

`analytics_insights` has `affected_dim = 'thumbnail'` but stores only a scalar `signal_strength` with no join back to which candidate caused it.

### Required State

Per `docs/learning-intelligence-layer.md` §3 (Thumbnail Recommendations) and §5 (Thumbnail Intelligence Agent), the intelligence layer needs:

**On `thumbnail_candidates`:**
- `style_tags` — TEXT JSON array (e.g., `["face_present", "high_contrast", "text_top"]`)
- `face_present` — INTEGER 0/1
- `color_scheme` — TEXT (e.g., `"dark_bg_light_text"`)
- `text_word_count` — INTEGER
- `composition_type` — TEXT (e.g., `"close_up | wide | split_panel | minimal"`)
- `ctr_outcome` — REAL, populated post-publish from `platform_analytics.ctr` where this candidate was selected
- `performance_band` — TEXT, populated post-publish
- `outcome_episode_views` — INTEGER, views at the time CTR was measured
- `outcome_linked_at` — TEXT timestamp when outcome was written back

**New table `thumbnail_performance`** — one row per thumbnail candidate × platform, storing the linkback from analytics to design:

```sql
CREATE TABLE IF NOT EXISTS thumbnail_performance (
    perf_id             TEXT NOT NULL PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    episode_id          TEXT NOT NULL,
    platform            TEXT NOT NULL,
    record_id           TEXT,           -- FK to platform_analytics
    ctr                 REAL,
    performance_band    TEXT,
    impressions         INTEGER,
    views               INTEGER,
    linked_at           TEXT NOT NULL,
    metadata_version    TEXT NOT NULL DEFAULT 'v3',
    FOREIGN KEY (candidate_id) REFERENCES thumbnail_candidates(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_tp_candidate  ON thumbnail_performance(candidate_id);
CREATE INDEX IF NOT EXISTS idx_tp_episode    ON thumbnail_performance(episode_id);
CREATE INDEX IF NOT EXISTS idx_tp_platform   ON thumbnail_performance(platform);
```

**New view:**
```sql
CREATE VIEW IF NOT EXISTS v_thumbnail_intelligence AS
SELECT
    tc.candidate_id,
    tc.episode_id,
    tc.rank,
    tc.selected,
    tc.text_overlay,
    tc.placement,
    tc.visual_prompt,
    tc.style_tags,
    tc.face_present,
    tc.composition_type,
    tc.ctr_outcome,
    tc.performance_band,
    tp.platform,
    tp.impressions,
    tp.views
FROM thumbnail_candidates tc
LEFT JOIN thumbnail_performance tp ON tp.candidate_id = tc.candidate_id
WHERE tc.selected = 1;
```

### Migration Required

**ALTER TABLE `thumbnail_candidates`** (8 columns):

```sql
ALTER TABLE thumbnail_candidates ADD COLUMN style_tags           TEXT;   -- JSON array
ALTER TABLE thumbnail_candidates ADD COLUMN face_present         INTEGER;
ALTER TABLE thumbnail_candidates ADD COLUMN color_scheme         TEXT;
ALTER TABLE thumbnail_candidates ADD COLUMN text_word_count      INTEGER;
ALTER TABLE thumbnail_candidates ADD COLUMN composition_type     TEXT;
ALTER TABLE thumbnail_candidates ADD COLUMN ctr_outcome          REAL;
ALTER TABLE thumbnail_candidates ADD COLUMN performance_band     TEXT;
ALTER TABLE thumbnail_candidates ADD COLUMN outcome_episode_views INTEGER;
ALTER TABLE thumbnail_candidates ADD COLUMN outcome_linked_at    TEXT;
```

**Create `thumbnail_performance` table** (new).

**Add `thumbnail_performance` CREATE TABLE** to `schema.sql` under v3 section.

**Add to `_V2_COLUMN_MIGRATIONS`** under `"thumbnail_candidates"` key.

### Dashboard Impact

- **Thumbnail Intelligence Agent** — cannot operate without `style_tags` and `ctr_outcome`; has no evidence to classify what visual strategies work
- **Learning page** — thumbnail recommendations will have zero evidence until outcome linkback is wired
- **Channel analytics — thumbnail panel** — cannot show `style → CTR` correlation
- **Archive Recall** — `archive_index.performance_signal` is a scalar that cannot differentiate thumbnail style from outcome; requires `thumbnail_performance` join

---

## 3. Title Intelligence

### Current State

`titles` stores: `title_id`, `run_id`, `episode_id`, `title_text`, `selected`, `created_at`.

No performance linkback. No title structure classification. No CTR outcome.

### Required State

Per `docs/learning-intelligence-layer.md` §3 (Title Recommendations), the Title Intelligence Agent requires:

**On `titles`:**
- `title_type` — TEXT (`question | list | statement | mystery | how_to | number_lead`)
- `char_count` — INTEGER (auto-computable but useful pre-stored)
- `word_count` — INTEGER
- `contains_number` — INTEGER 0/1 (e.g., "5 Secrets of...")
- `sentiment` — TEXT (`neutral | intriguing | alarming | inspiring`)
- `ctr_outcome` — REAL, populated post-publish from `platform_analytics.ctr`
- `performance_band` — TEXT, populated post-publish
- `outcome_linked_at` — TEXT timestamp

### Migration Required

**ALTER TABLE `titles`** (7 columns):

```sql
ALTER TABLE titles ADD COLUMN title_type        TEXT;
ALTER TABLE titles ADD COLUMN char_count        INTEGER;
ALTER TABLE titles ADD COLUMN word_count        INTEGER;
ALTER TABLE titles ADD COLUMN contains_number   INTEGER DEFAULT 0;
ALTER TABLE titles ADD COLUMN sentiment         TEXT;
ALTER TABLE titles ADD COLUMN ctr_outcome       REAL;
ALTER TABLE titles ADD COLUMN performance_band  TEXT;
ALTER TABLE titles ADD COLUMN outcome_linked_at TEXT;
```

**Add to `_V2_COLUMN_MIGRATIONS`** under `"titles"` key.

**New view:**
```sql
CREATE VIEW IF NOT EXISTS v_title_intelligence AS
SELECT
    t.title_id,
    t.episode_id,
    t.title_text,
    t.selected,
    t.title_type,
    t.char_count,
    t.word_count,
    t.contains_number,
    t.sentiment,
    t.ctr_outcome,
    t.performance_band,
    pa.views          AS episode_views,
    pa.impressions    AS episode_impressions,
    e.topic           AS episode_topic
FROM titles t
LEFT JOIN episodes         e  ON e.episode_id  = t.episode_id
LEFT JOIN platform_analytics pa ON pa.episode_id = t.episode_id
                                AND pa.platform  = 'youtube'
WHERE t.selected = 1;
```

### Dashboard Impact

- **Title Intelligence Agent** — zero training signal until `ctr_outcome` and `title_type` are populated
- **Learning page — title recommendations** — cannot distinguish question-format titles from statement titles in evidence
- **A/B title testing** — not supportable without structure classification; all titles look identical to the intelligence layer

---

## 4. Campaign Intelligence

### Current State

**There is no campaign concept in the schema.** The closest constructs are:

- `episode_topics` — a backlog/queue with `topic_text` and `priority`, but no grouping above individual topic
- `episodes` — flat list with no series or campaign grouping
- `signal_windows` — per-episode, per-YouTube-video time-series with no cross-episode campaign view

The current schema treats every episode as independent. There is no way to:
- Group episodes into a campaign/series
- Track campaign-level performance (total views across N episodes)
- Assign a shared campaign goal to a group of episodes
- Measure campaign completion rate

### Required State

Campaign Intelligence requires a two-layer addition:

**New table `campaigns`** — one row per campaign/series:

```sql
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id         TEXT NOT NULL PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    campaign_name       TEXT NOT NULL,
    campaign_type       TEXT NOT NULL DEFAULT 'series'
                             CHECK(campaign_type IN ('series','topic_cluster','seasonal','challenge','evergreen')),
    status              TEXT NOT NULL DEFAULT 'active'
                             CHECK(status IN ('draft','active','complete','archived')),
    episode_target      INTEGER,            -- how many episodes planned
    topic_theme         TEXT,               -- unifying theme / keyword cluster
    publish_start       TEXT,               -- ISO 8601 date — first episode publish date
    publish_end         TEXT,               -- ISO 8601 date — last episode publish date (actual or target)
    views_target        INTEGER,            -- aggregate view goal for the campaign
    ctr_target          REAL,               -- target average CTR across episodes
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    metadata_version    TEXT NOT NULL DEFAULT 'v3'
);

CREATE INDEX IF NOT EXISTS idx_campaigns_channel ON campaigns(channel_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status  ON campaigns(status);
```

**New table `campaign_episodes`** — many-to-many junction:

```sql
CREATE TABLE IF NOT EXISTS campaign_episodes (
    ce_id               TEXT NOT NULL PRIMARY KEY,
    campaign_id         TEXT NOT NULL REFERENCES campaigns(campaign_id),
    episode_id          TEXT NOT NULL,
    episode_order       INTEGER NOT NULL DEFAULT 0,  -- position within the campaign
    added_at            TEXT NOT NULL,
    metadata_version    TEXT NOT NULL DEFAULT 'v3'
);

CREATE INDEX IF NOT EXISTS idx_ce_campaign ON campaign_episodes(campaign_id);
CREATE INDEX IF NOT EXISTS idx_ce_episode  ON campaign_episodes(episode_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ce_unique ON campaign_episodes(campaign_id, episode_id);
```

**Add `campaign_id` to `episode_topics`:**

```sql
ALTER TABLE episode_topics ADD COLUMN campaign_id TEXT;
```

**New view `v_campaign_intelligence`:**

```sql
CREATE VIEW IF NOT EXISTS v_campaign_intelligence AS
SELECT
    c.campaign_id,
    c.channel_id,
    c.campaign_name,
    c.campaign_type,
    c.status,
    c.episode_target,
    c.views_target,
    c.ctr_target,
    COUNT(ce.episode_id)                     AS episodes_linked,
    SUM(pa.views)                            AS total_views,
    ROUND(AVG(pa.ctr), 4)                   AS avg_ctr,
    ROUND(AVG(pa.avg_view_pct), 4)          AS avg_retention,
    ROUND(AVG(pa.completion_rate), 4)       AS avg_completion_rate,
    MIN(pa.snapshot_date)                   AS first_data_date,
    MAX(pa.snapshot_date)                   AS latest_data_date
FROM campaigns c
LEFT JOIN campaign_episodes  ce ON ce.campaign_id  = c.campaign_id
LEFT JOIN platform_analytics pa ON pa.episode_id   = ce.episode_id
                                AND pa.platform     = 'youtube'
GROUP BY c.campaign_id;
```

Note: `v_campaign_intelligence` depends on `platform_analytics.completion_rate` from the Episode Intelligence migration (§1).

### Migration Required

1. **New table `campaigns`** — add to `schema.sql` under v3 section
2. **New table `campaign_episodes`** — add to `schema.sql` under v3 section
3. **ALTER TABLE `episode_topics` ADD COLUMN `campaign_id` TEXT** — add to `_V2_COLUMN_MIGRATIONS`
4. **New view `v_campaign_intelligence`**

### Dashboard Impact

- **Campaign tracking panel** — entirely missing without this table; no surface to plan or track a series of episodes
- **Topic Intelligence Agent** — cannot identify topic cluster performance without campaign grouping
- **Factory Strategist** — cross-episode pattern recognition requires campaign context; without it, all episodes appear atomic
- **Publishing cadence** — schedule recommendations based on campaign cadence cannot be computed
- **Analytics → channel analytics tab** — campaign-level CTR trend is invisible; each episode is treated as independent

---

## 5. Revenue Intelligence

### Current State

Revenue data is **not stored anywhere** in the schema:

- `platform_analytics` has no `revenue_usd`, `cpm`, or `rpm` column
- `channel_profile_settings` has frequency targets (`yt_videos_per_week`, etc.) but no monetization goals
- `signal_windows` has no revenue columns
- `learning_records` could store revenue signals generically, but there is no schema to enforce it

### Required State

Per `docs/analytics-layer-design.md` §4.1 (revenue_usd as a core metric), §4.4 (monetization_goals tracking), and §6.2 (ChannelAnalytics), Revenue Intelligence requires:

**On `platform_analytics`** (4 columns — part of Episode Intelligence migration, listed separately for clarity):
- `revenue_usd` — REAL, revenue attributed to this episode for the measurement window
- `cpm` — REAL, cost per mille (USD per 1000 views) for this window
- `rpm` — REAL, revenue per mille (creator's actual earnings rate)
- `monetization_eligible` — INTEGER 0/1, whether the video is monetized

**On `channel_profile_settings`** (monetization goals — 6 columns):
- `monetization_status` — TEXT (`not_monetized | eligible | monetized`)
- `revenue_model` — TEXT JSON array (e.g., `["adsense","sponsorship"]`)
- `cpm_target` — REAL (target CPM in USD)
- `monthly_revenue_target` — REAL (target monthly revenue in USD)
- `monetization_started_at` — TEXT (date monetization was enabled)
- `sponsorship_policy` — TEXT (free-text policy string)

**New view `v_revenue_intelligence`:**

```sql
CREATE VIEW IF NOT EXISTS v_revenue_intelligence AS
SELECT
    pa.episode_id,
    pa.channel_id,
    e.title                             AS episode_title,
    e.published_at                      AS episode_published_at,
    pa.platform,
    pa.snapshot_date,
    pa.window_days,
    pa.views,
    pa.revenue_usd,
    pa.cpm,
    pa.rpm,
    pa.monetization_eligible,
    cps.monetization_status,
    cps.cpm_target,
    cps.monthly_revenue_target,
    CASE
        WHEN cps.cpm_target IS NOT NULL AND pa.cpm IS NOT NULL
        THEN ROUND((pa.cpm / cps.cpm_target) * 100.0, 1)
        ELSE NULL
    END                                 AS cpm_vs_target_pct
FROM platform_analytics pa
LEFT JOIN episodes              e   ON e.episode_id = pa.episode_id
LEFT JOIN channel_profile_settings cps ON 1=1      -- single-row table
WHERE pa.monetization_eligible = 1
   OR pa.revenue_usd IS NOT NULL;
```

### Migration Required

**ALTER TABLE `platform_analytics`** (4 columns — coordinate with §1 migration):

```sql
ALTER TABLE platform_analytics ADD COLUMN revenue_usd           REAL;
ALTER TABLE platform_analytics ADD COLUMN cpm                   REAL;
ALTER TABLE platform_analytics ADD COLUMN rpm                   REAL;
ALTER TABLE platform_analytics ADD COLUMN monetization_eligible INTEGER DEFAULT 0;
```

**ALTER TABLE `channel_profile_settings`** (6 columns):

```sql
ALTER TABLE channel_profile_settings ADD COLUMN monetization_status     TEXT DEFAULT 'not_monetized';
ALTER TABLE channel_profile_settings ADD COLUMN revenue_model           TEXT;   -- JSON array
ALTER TABLE channel_profile_settings ADD COLUMN cpm_target              REAL;
ALTER TABLE channel_profile_settings ADD COLUMN monthly_revenue_target  REAL;
ALTER TABLE channel_profile_settings ADD COLUMN monetization_started_at TEXT;
ALTER TABLE channel_profile_settings ADD COLUMN sponsorship_policy      TEXT;
```

**Add both to `_V2_COLUMN_MIGRATIONS` in `db.py`** under `"platform_analytics"` and `"channel_profile_settings"` keys respectively.

### Dashboard Impact

- **Home KPI strip** — `total_revenue_usd` and `cpm_target` goal tracking cannot render without these columns
- **Channel analytics — monetization panel** — entirely blocked; revenue data lives only in `raw_json` today which cannot be queried
- **Revenue Intelligence Agent** — cannot generate CPM trend or monthly revenue trajectory without structured columns
- **Goal tracking (`v_goal_tracking`)** — `monetization_goals.cpm_target` comparison is impossible without `platform_analytics.cpm`
- **Role-based revenue visibility** — enforcement layer needs a queryable column; cannot apply `revenue_visible: false` logic on `raw_json`

---

## Migration Runbook

### Phase 1 — Platform Analytics Extension (Episode + Revenue Intelligence)

All `platform_analytics` column additions in one migration block. Order matters: Episode Intelligence columns (§1) and Revenue Intelligence columns (§5) must be added together to avoid running `_V2_COLUMN_MIGRATIONS` twice on the same table.

**Combined `platform_analytics` additions (24 columns total):**

```python
"platform_analytics": [
    # Episode Intelligence (§1)
    ("channel_id",              "TEXT"),
    ("subscriber_gain",         "INTEGER"),
    ("traffic_sources",         "TEXT"),
    ("retention_curve",         "TEXT"),
    ("retention_at_30s",        "REAL"),
    ("retention_at_midpoint",   "REAL"),
    ("completion_rate",         "REAL"),
    ("engagement_rate",         "REAL"),
    ("relative_ctr",            "REAL"),
    ("relative_retention",      "REAL"),
    ("performance_band_ctr",    "TEXT"),
    ("performance_band_retention", "TEXT"),
    ("analytics_status",        "TEXT DEFAULT 'active'"),
    ("stabilization_at",        "TEXT"),
    ("all_platforms_ingested",  "INTEGER DEFAULT 0"),
    ("platforms_pending",       "TEXT"),
    ("analytics_period_start",  "TEXT"),
    ("analytics_period_end",    "TEXT"),
    ("anomalies_json",          "TEXT"),
    # Revenue Intelligence (§5)
    ("revenue_usd",             "REAL"),
    ("cpm",                     "REAL"),
    ("rpm",                     "REAL"),
    ("monetization_eligible",   "INTEGER DEFAULT 0"),
]
```

### Phase 2 — Thumbnail + Title Outcome Linkback

```python
"thumbnail_candidates": [
    ("style_tags",          "TEXT"),
    ("face_present",        "INTEGER"),
    ("color_scheme",        "TEXT"),
    ("text_word_count",     "INTEGER"),
    ("composition_type",    "TEXT"),
    ("ctr_outcome",         "REAL"),
    ("performance_band",    "TEXT"),
    ("outcome_episode_views", "INTEGER"),
    ("outcome_linked_at",   "TEXT"),
],
"titles": [
    ("title_type",          "TEXT"),
    ("char_count",          "INTEGER"),
    ("word_count",          "INTEGER"),
    ("contains_number",     "INTEGER DEFAULT 0"),
    ("sentiment",           "TEXT"),
    ("ctr_outcome",         "REAL"),
    ("performance_band",    "TEXT"),
    ("outcome_linked_at",   "TEXT"),
],
```

**Create `thumbnail_performance` table** in `schema.sql`.

### Phase 3 — Campaign Entity

Add `campaigns` and `campaign_episodes` tables to `schema.sql`.

Add to `_V2_COLUMN_MIGRATIONS`:
```python
"episode_topics": [
    ("campaign_id", "TEXT"),
],
```

### Phase 4 — Channel Profile Monetization Goals

```python
"channel_profile_settings": [
    ("monetization_status",     "TEXT DEFAULT 'not_monetized'"),
    ("revenue_model",           "TEXT"),
    ("cpm_target",              "REAL"),
    ("monthly_revenue_target",  "REAL"),
    ("monetization_started_at", "TEXT"),
    ("sponsorship_policy",      "TEXT"),
],
```

### Phase 5 — Views

Add to `schema.sql`:
- `v_episode_intelligence`
- `v_thumbnail_intelligence`
- `v_title_intelligence`
- `v_campaign_intelligence`
- `v_revenue_intelligence`

---

## Field Count Summary

| Migration Phase | Table | Columns Added | New Tables | New Views |
|---|---|---|---|---|
| 1 — Episode + Revenue | `platform_analytics` | 23 | 0 | 2 |
| 2 — Thumbnail Intelligence | `thumbnail_candidates` | 9 | 1 (`thumbnail_performance`) | 1 |
| 2 — Title Intelligence | `titles` | 8 | 0 | 1 |
| 3 — Campaign Intelligence | `campaigns`, `campaign_episodes` | 0 | 2 | 1 |
| 3 — Campaign linkage | `episode_topics` | 1 | 0 | 0 |
| 4 — Revenue goals | `channel_profile_settings` | 6 | 0 | 1 |
| **Total** | | **47** | **3** | **6** |

---

## Risk Register

### R1 — `platform_analytics` UNIQUE constraint breaks post-migration

**Risk:** The existing `UNIQUE(episode_id, platform, snapshot_date)` constraint covers the entire row. Adding `analytics_status` with a default of `'active'` is safe (no uniqueness impact), but `analytics_period_start` / `analytics_period_end` columns must not be added to the UNIQUE index.

**Mitigation:** Do not alter the UNIQUE constraint. The new columns are nullable or defaulted. The constraint remains `(episode_id, platform, snapshot_date)`.

### R2 — `v_campaign_intelligence` depends on Episode Intelligence migration

**Risk:** `completion_rate` is referenced in `v_campaign_intelligence` but only exists after Phase 1 migration. If views are created before Phase 1 runs, the view creation will fail.

**Mitigation:** Apply migrations in strict phase order: Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 (views last).

### R3 — Outcome linkback requires a backfill job

**Risk:** `thumbnail_candidates.ctr_outcome` and `titles.ctr_outcome` will be NULL for all historical rows until a backfill job joins `platform_analytics` → selected candidate. The intelligence agents cannot be run until this backfill completes.

**Mitigation:** Write a one-time `backfill_outcome_linkback()` utility in `db.py` that runs after Phase 2 migration. This is a read-join-write operation with no destructive steps. Can be re-run idempotently.

### R4 — `channel_id` absent from `platform_analytics` prevents baseline computation

**Risk:** `relative_ctr` and `performance_band_*` require the channel's 30-day CTR average. Without `channel_id` on `platform_analytics`, there is no way to identify which rows belong to the same channel. The episode → run → channel_id path exists (`episodes.run_id` → `runs.channel_id` via v2 migration) but requires a join that currently no analytics query makes.

**Mitigation:** Phase 1 migration adds `channel_id` to `platform_analytics`. Backfill via `UPDATE platform_analytics SET channel_id = (SELECT r.channel_id FROM runs r JOIN episodes e ON e.run_id = r.run_id WHERE e.episode_id = platform_analytics.episode_id LIMIT 1)`.

### R5 — `campaigns` table is a new concept with no existing data

**Risk:** Campaign Intelligence cannot surface any insights until campaigns are manually defined and episodes are linked. The intelligence layer will have an empty `campaigns` table post-migration and will produce no campaign signals until an operator creates at least one campaign.

**Mitigation:** Document this as expected behavior. Add Campaign creation to the F11 operator onboarding flow. The schema migration is non-blocking — all other tables continue to function.

---

## Priority Order

| Priority | Migration | Reason |
|---|---|---|
| 1 | Phase 1 (platform_analytics) | Unblocks all five domains; no derived metrics or revenue possible without it |
| 2 | Phase 4 (channel_profile_settings) | Revenue goal tracking blocked; dashboard KPI strip empty without monetization goals |
| 3 | Phase 2 (thumbnail + title outcome linkback) | Intelligence agents cannot train without outcome data |
| 4 | Phase 3 (campaigns) | New capability; no existing data to backfill |
| 5 | Phase 5 (views) | Final step; all views depend on prior migrations |

---

*F11_GAP_ANALYSIS.md — generated 2026-06-19 for F11 prep*
