# Analytics Layer Design

**Phase:** B5.8  
**Depends on:** B5 (Dashboard Architecture), B5.275 (OIS), B5.7 (Learning Module), B4.75 (Channel Profile), B7 (Run OS)  
**Feeds into:** Learning Module (performance signals), OIS (anomaly signals), Dashboard (performance visibility), Channel Profile (goal tracking)

---

## 1. Purpose and Position

The Analytics Layer is the explanatory layer of Factory OS. It answers: what happened?

```
Production Runs       →  What was produced?
Analytics Layer       →  How did it perform?
Learning Module       →  What might improve?
```

The Analytics Layer ingests performance data from external platforms and internal factory systems, normalizes it into a consistent data model, tracks performance against Channel Profile goals, and surfaces anomalies to OIS. It does not generate recommendations. It does not decide what to do with the data. It provides data.

### 1.1 Scope

The Analytics Layer covers three distinct performance surfaces:

**Episode performance** — how each published episode performs on the platforms it was published to: views, watch time, retention, CTR, impressions, revenue.

**Channel performance** — aggregate channel-level metrics: subscriber growth, total watch hours, overall CTR, platform-level engagement trends, revenue trajectory.

**Production performance** — how the factory's internal production pipeline is performing: cost per episode, pipeline success rate, gate pass rates, stage latency, agent quality scores. This is internal analytics, not platform analytics.

### 1.2 What Analytics does not own

| Concern | Owner |
|---|---|
| Generating recommendations from data | Learning Module |
| Deciding whether data warrants operator attention | OIS |
| Delivering performance notifications | Notification System |
| Analyzing why something happened (root cause) | Learning Module intelligence agents |
| Defining targets and goals | Channel Profile |
| Executing changes based on performance | Downstream systems via Approval System |

---

## 2. Architecture

### 2.1 Inputs

| Source | Data type | Cadence |
|---|---|---|
| YouTube Analytics API | Views, watch time, CTR, impressions, retention curves, subscriber events, revenue (if monetized) | Daily batch. Min 48h stabilization before retention curves are ingested. |
| TikTok Analytics API | Views, watch completion rate, shares, comments, follower events, FYP vs. following traffic split | Daily batch. Min 24h stabilization. |
| Instagram/Meta Analytics API | Reel views, saves, shares, reach, engagement rate, story exits, link clicks, audience demographics | Daily batch. Min 24h stabilization. |
| Additional platforms | Per platform configuration. Same pattern: daily batch with platform-specific stabilization window. | Configurable |
| Run OS (internal) | Stage completion times, retry counts, gate pass/fail per stage, agent cost per stage, total episode cost | Near-real-time. Available within 1 minute of run completion. |
| Acceptance Audit (internal) | Gate verdict history, BGA pass/fail rates, QA score distributions, compliance failure rates | Near-real-time. Written as gates complete. |
| Cost Ledger (internal) | Per-episode cost by agent, per-run cost, budget utilization, credit burn rate | Near-real-time. |
| Channel Profile | Goals (publishing_goals, monetization_goals) against which performance is measured | Read at analysis time. |

### 2.2 Outputs

| Output | Destination |
|---|---|
| `episode_analytics` records | Analytics store, Learning Module (via signals), Dashboard |
| `channel_analytics` records | Analytics store, Learning Module (via signals), Dashboard |
| `production_analytics` records | Analytics store, Learning Module (via signals), Dashboard |
| `goal_tracking` records | Analytics store, Dashboard |
| `analytics_signal` | OIS (for anomaly routing) |
| `performance_summary` | Dashboard Director (for Home page KPI display) |
| `ingestion_health_status` | OIS (if an ingestion source fails or is stale) |

### 2.3 Data flow

```
Platform APIs → Analytics Layer Ingestion
                  → normalize to AnalyticsRecord
                  → apply stabilization window check
                  → compute derived metrics (AVD, retention drop-off points)
                  → store EpisodeAnalytics / ChannelAnalytics records
                  → compare against Channel Profile goals → GoalTracking records
                  → detect anomalies → submit analytics_signal to OIS
                  → submit performance signals to Learning Module (via signal API)

Run OS / Acceptance Audit / Cost Ledger → Analytics Layer Ingestion
                  → normalize to ProductionAnalyticsRecord
                  → compute trends (cost trend, gate pass rate trend)
                  → store ProductionAnalytics records
                  → detect anomalies → submit analytics_signal to OIS
                  → submit production signals to Learning Module
```

---

## 3. Ingestion Model

### 3.1 Batch ingestion

Platform analytics are ingested on a daily batch cycle. No platform analytics are ingested in real-time. The batch window runs once per day per platform, pulling the previous day's data.

Stabilization windows apply before data is included in analysis:
- YouTube: 48 hours from publish before retention curve data is ingested (YouTube's algorithm stabilizes view counts over the first 48 hours)
- TikTok: 24 hours from publish before any metrics are ingested
- Instagram: 24 hours from publish before any metrics are ingested

Before the stabilization window has elapsed, episode analytics records exist but are marked `status: pending_stabilization`. These records are not included in Learning Module signals or goal tracking calculations. They are visible in the dashboard as "data pending" with an estimated stabilization time.

### 3.2 Internal data ingestion

Internal factory data (Run OS, Acceptance Audit, Cost Ledger) is ingested near-real-time. There is no stabilization window for internal data — a run that completes is immediately available in production analytics.

### 3.3 Ingestion idempotency

Every ingestion operation is idempotent. Re-ingesting the same data window produces the same records. Platform API data is identified by `(platform, episode_id, measured_date)` — duplicate ingestion within the same window updates the existing record rather than creating a new one.

### 3.4 Ingestion failure handling

If a platform API ingestion fails:
- The failure is logged in the ingestion health record
- The missing data window is marked `status: ingestion_failed`
- An `ingestion_health_status` signal is submitted to OIS
- OIS routes this as a WARNING (not CRITICAL — production is not blocked by analytics ingestion failure)
- The Analytics Layer retries ingestion on the next batch cycle
- Historical gaps are filled when ingestion succeeds — the Analytics Layer can backfill up to 14 days of missing data

If a platform API ingestion fails for more than 7 consecutive days, OIS escalates to CRITICAL and notifies the Operator.

### 3.5 Data completeness tracking

Every AnalyticsRecord tracks its own completeness:
- `all_platforms_ingested`: boolean — whether analytics have arrived from all enabled platforms
- `platforms_pending`: list of platforms still awaiting data
- `oldest_pending_data_age_hours`: how long the oldest pending data has been outstanding

Goal tracking and Learning Module signal submission do not proceed for an episode until `all_platforms_ingested = true` (or until a configured per-platform timeout is exceeded and the episode is marked complete without that platform's data).

---

## 4. Analytics Domains

### 4.1 Episode performance

Per-episode performance metrics for each platform the episode was published to.

**Core metrics:**
- `views`: total view count
- `impressions`: total impressions served
- `ctr`: click-through rate (views / impressions)
- `watch_time_hours`: total accumulated watch hours
- `avg_view_duration_seconds`: average session duration
- `avg_view_percentage`: average percentage of the episode watched
- `retention_curve`: array of percentage values at 10% time intervals (10 points for a full episode) — or by timestamp if platform provides timestamp-level data
- `subscriber_gain`: subscribers gained from this episode
- `revenue_usd`: revenue attributed to this episode (null if not monetized)
- `like_count`, `comment_count`, `share_count`, `save_count`: engagement counts where platform provides them
- `traffic_sources`: breakdown of where views came from (search, browse, direct, algorithm, etc.)

**Derived metrics (computed by Analytics Layer):**
- `retention_at_30s`: retention at the 30-second mark (computed from retention_curve)
- `retention_at_midpoint`: retention at 50% of episode duration
- `completion_rate`: percentage of viewers who watched ≥ 80% of the episode
- `engagement_rate`: (likes + comments + shares) / views — where available
- `relative_ctr`: this episode's CTR vs. channel's 30-day average CTR (percent difference)
- `relative_retention`: this episode's avg_view_percentage vs. channel's 30-day average

**Performance bands (applied to relative metrics):**

| Band | Threshold | Meaning |
|---|---|---|
| `outperforming` | > +20% vs. channel average | Significantly above channel baseline |
| `above_average` | +5% to +20% | Modestly above baseline |
| `on_baseline` | -5% to +5% | Within normal variation |
| `below_average` | -5% to -20% | Modestly below baseline |
| `underperforming` | < -20% vs. channel average | Significantly below channel baseline |

Performance bands require a minimum of 5 prior episodes to establish a channel average. Before 5 episodes, all relative metrics are `null` and performance bands are not computed.

### 4.2 Channel performance

Aggregate channel-level metrics tracked over time.

**Core metrics (per platform, per period):**
- `total_views`: sum of all episode views in the period
- `total_watch_time_hours`: sum of all watch hours
- `total_impressions`: sum of all impressions
- `channel_ctr`: aggregate CTR (total views / total impressions)
- `subscriber_count`: current subscriber/follower count
- `subscriber_growth`: net subscribers gained in the period
- `subscriber_growth_rate`: percentage growth vs. prior period
- `total_revenue_usd`: total channel revenue in the period (null if not monetized)
- `avg_episode_ctr`: average CTR across all episodes in the period
- `avg_episode_retention`: average view percentage across all episodes in the period
- `top_performing_episode_ids`: top 3 episodes by views in the period
- `underperforming_episode_ids`: bottom 3 episodes by relative_ctr in the period

**Tracking periods:** Daily snapshot, weekly rollup, monthly rollup. The Analytics Layer maintains all three granularities independently.

### 4.3 Production performance

Internal factory pipeline performance metrics.

**Per-episode production metrics:**
- `total_production_cost_usd`: total cost across all agents for this episode
- `total_production_time_minutes`: elapsed time from run start to run completion
- `pipeline_success`: boolean — did the episode complete all stages without terminal failure?
- `retry_count`: total number of retried tasks across all stages
- `gate_verdicts_summary`: pass/fail count per gate type
- `bga_pass_rate`: fraction of BGA gates passed on first attempt (Pass 1 and Pass 2)
- `qa_score`: QA Agent overall quality score
- `compliance_passed`: boolean
- `stage_costs`: map of stage_name → cost_usd
- `stage_latencies`: map of stage_name → elapsed_seconds
- `human_review_age_hours`: time from episode entering review queue to final review decision

**Per-channel production trends (30-day rolling):**
- `avg_cost_per_episode`: rolling average
- `cost_trend`: linear trend (increasing / stable / decreasing)
- `gate_pass_rate_by_type`: map of gate_type → pass rate
- `pipeline_success_rate`: fraction of runs completing all stages
- `avg_human_review_time_hours`: rolling average
- `budget_utilization`: (total spend in period) / (budget ceiling × run count) as a percentage

### 4.4 Goal tracking

Goal tracking compares Analytics Layer data against the targets defined in the Channel Profile (`publishing_goals`, `monetization_goals`).

**Tracked goals:**

| Profile field | Tracking metric | Evaluation cadence |
|---|---|---|
| `publishing_goals.episodes_per_month` | Episodes published in current calendar month | Monthly |
| `publishing_goals.growth_target` | Current subscriber count vs. target and progress rate | Weekly |
| `publishing_goals.engagement_target` | 30-day average engagement rate vs. target | Weekly |
| `publishing_goals.ctr_target` | 30-day average CTR vs. target | Weekly |
| `monetization_goals.cpm_target` | 30-day average CPM vs. target (null if not monetized) | Monthly |
| `monetization_goals.monthly_revenue_target` | Monthly revenue vs. target (null if not monetized) | Monthly |

**Goal status classifications:**

| Status | Criteria |
|---|---|
| `on_track` | Current trajectory will reach the target by the target date at current rate |
| `at_risk` | Current trajectory will miss the target by < 20% at current rate |
| `off_track` | Current trajectory will miss the target by ≥ 20% at current rate |
| `exceeded` | Target already met or exceeded |
| `not_enough_data` | Fewer than 3 data points for this goal (new channel or recently configured goal) |

**Goal drift alerts:** When a goal's status changes from `on_track` to `at_risk` or from `at_risk` to `off_track`, the Analytics Layer submits an `analytics_signal` to OIS of type `goal_drift`. OIS routes this as a WARNING and routes it to the relevant notification recipients (OIS classification rules apply — it may be a dashboard notification only, not an external notification).

---

## 5. Anomaly Detection

The Analytics Layer performs lightweight anomaly detection on ingested metrics. It does not perform root cause analysis (that is the Learning Module's role). It identifies deviations from expected ranges and submits signals.

### 5.1 Anomaly types

| Anomaly type | Trigger condition | OIS signal severity |
|---|---|---|
| `ctr_outlier_high` | Episode CTR > 2.5× channel 30-day average | INFO (positive) |
| `ctr_outlier_low` | Episode CTR < 0.4× channel 30-day average | WARNING |
| `retention_drop` | Episode retention_at_30s < 60% (absolute threshold, platform-adjusted) | WARNING |
| `retention_collapse` | Episode avg_view_percentage < 0.3× channel average | WARNING |
| `subscriber_spike` | Subscriber gain in 24h > 3× daily average | INFO (positive) |
| `subscriber_loss` | Net subscriber loss in 24h > 1× daily average gain | WARNING |
| `cost_spike` | Episode total_production_cost > 1.75× channel 30-day average | WARNING |
| `gate_failure_cluster` | Same gate type failed in ≥ 3 of last 5 runs | WARNING |
| `pipeline_failure_rate` | Pipeline success rate drops below 0.70 over 7-day window | CRITICAL |
| `ingestion_stale` | Platform ingestion > 36h without new data | WARNING |
| `goal_drift` | Goal status changes to at_risk or off_track | WARNING |
| `budget_velocity_high` | Budget burn rate will exhaust ceiling in < 7 days at current rate | WARNING (OIS routes to CRITICAL if production_blocked) |

### 5.2 Anomaly suppression

The Analytics Layer does not submit duplicate anomaly signals. If the same anomaly type for the same entity has been submitted within the deduplication window (24h for most types, 7 days for recurring cluster anomalies), the signal is suppressed. OIS also applies its own deduplication (B5.275 §9.6), providing a second layer.

### 5.3 Positive signals

Outperforming episodes and subscriber spikes generate `INFO` signals. OIS routes these as passive insights to the Learning page only — they are not surfaced on Home as alerts. The operator can see positive performance on their own terms (dashboard, channel analytics). Positive signals are most useful to the Learning Module as evidence for what worked.

---

## 6. Data Contracts

### 6.1 EpisodeAnalytics

```
EpisodeAnalytics {
  analytics_id:                string
  episode_id:                  string
  run_id:                      string
  channel_id:                  string
  platform:                    enum(youtube, tiktok, instagram, custom)
  published_at:                timestamp
  analytics_period_start:      timestamp
  analytics_period_end:        timestamp
  status:                      enum(pending_stabilization, active, final, ingestion_failed)
  stabilization_at:            timestamp | null   // when stabilization window was met
  all_platforms_ingested:      boolean
  platforms_pending:           string[]

  // Core metrics
  views:                       int | null
  impressions:                 int | null
  ctr:                         float | null      // 0.0–1.0
  watch_time_hours:            float | null
  avg_view_duration_seconds:   float | null
  avg_view_percentage:         float | null      // 0.0–1.0
  retention_curve:             float[] | null    // 10 points at 10% intervals
  subscriber_gain:             int | null
  revenue_usd:                 float | null
  like_count:                  int | null
  comment_count:               int | null
  share_count:                 int | null
  save_count:                  int | null
  traffic_sources:             map<source_type, float> | null  // % breakdown

  // Derived metrics (null until stabilized and channel baseline exists)
  retention_at_30s:            float | null
  retention_at_midpoint:       float | null
  completion_rate:             float | null
  engagement_rate:             float | null
  relative_ctr:                float | null      // % vs. channel 30-day average
  relative_retention:          float | null      // % vs. channel 30-day average
  performance_band_ctr:        enum(outperforming, above_average, on_baseline,
                                    below_average, underperforming) | null
  performance_band_retention:  enum(outperforming, above_average, on_baseline,
                                    below_average, underperforming) | null

  // Anomalies detected for this episode
  anomalies:                   string[]           // anomaly_type values

  ingested_at:                 timestamp
  last_updated_at:             timestamp
}
```

### 6.2 ChannelAnalytics

```
ChannelAnalytics {
  analytics_id:                string
  channel_id:                  string
  platform:                    string
  period_type:                 enum(daily, weekly, monthly)
  period_start:                date
  period_end:                  date

  total_views:                 int
  total_watch_time_hours:      float
  total_impressions:           int
  channel_ctr:                 float
  subscriber_count:            int
  subscriber_growth:           int
  subscriber_growth_rate:      float
  total_revenue_usd:           float | null
  avg_episode_ctr:             float
  avg_episode_retention:       float
  episodes_published:          int
  top_performing_episode_ids:  string[]
  underperforming_episode_ids: string[]

  // Goal tracking for this period
  goal_statuses:               map<goal_field, GoalStatus>

  // Trends
  ctr_trend:                   enum(improving, stable, declining) | null
  retention_trend:             enum(improving, stable, declining) | null
  subscriber_trend:            enum(improving, stable, declining) | null
  cost_per_episode_trend:      enum(improving, stable, declining) | null

  computed_at:                 timestamp
}
```

### 6.3 ProductionAnalytics

```
ProductionAnalytics {
  analytics_id:                string
  episode_id:                  string
  run_id:                      string
  channel_id:                  string

  // Episode production metrics
  total_production_cost_usd:   float
  total_production_time_minutes: float
  pipeline_success:            boolean
  retry_count:                 int
  bga_pass_rate:               float             // 0.0–1.0
  qa_score:                    float | null
  compliance_passed:           boolean
  stage_costs:                 map<stage_name, float>
  stage_latencies:             map<stage_name, float>
  human_review_age_hours:      float | null

  // Gate verdicts summary
  gate_verdicts_summary: {
    passed:                    int
    failed:                    int
    passed_with_warnings:      int
    overrides_applied:         int
  }

  ingested_at:                 timestamp
}
```

### 6.4 ChannelProductionAnalytics

```
ChannelProductionAnalytics {
  analytics_id:                string
  channel_id:                  string
  period_type:                 enum(daily, weekly, monthly)
  period_start:                date
  period_end:                  date

  episodes_started:            int
  episodes_completed:          int
  pipeline_success_rate:       float
  avg_cost_per_episode:        float
  cost_trend:                  enum(increasing, stable, decreasing)
  total_production_cost_usd:   float
  budget_utilization:          float             // 0.0–1.0
  avg_human_review_time_hours: float | null
  gate_pass_rate_by_type:      map<gate_type, float>
  retry_rate:                  float             // retries / total tasks
  avg_stage_latency:           map<stage_name, float>

  computed_at:                 timestamp
}
```

### 6.5 GoalTracking

```
GoalTracking {
  goal_tracking_id:            string
  channel_id:                  string
  goal_field:                  string            // e.g., "publishing_goals.episodes_per_month"
  goal_target:                 object            // the target value from Channel Profile
  goal_target_date:            date | null
  current_value:               object            // current measured value
  status:                      enum(on_track, at_risk, off_track, exceeded, not_enough_data)
  trajectory:                  string | null     // e.g., "at current rate: 7,200 subscribers by 2026-12-31 vs. target 10,000"
  data_points:                 int               // how many data points behind this assessment
  evaluated_at:                timestamp
}
```

### 6.6 AnalyticsSignal

```
AnalyticsSignal {
  signal_id:                   string
  channel_id:                  string
  episode_id:                  string | null     // null for channel-level signals
  signal_type:                 string            // anomaly_type or "goal_drift" or "ingestion_stale"
  severity:                    enum(INFO, WARNING, CRITICAL)
  description:                 string            // human-readable description
  metric_name:                 string            // which metric triggered this
  observed_value:              object            // the value that triggered
  baseline_value:              object | null     // the baseline it was compared against
  emitted_at:                  timestamp
  deduplication_key:           string            // (channel_id + signal_type + episode_id + day) — prevents duplicate submission
}
```

### 6.7 IngestionHealthRecord

```
IngestionHealthRecord {
  health_id:                   string
  channel_id:                  string
  platform:                    string
  ingestion_date:              date
  status:                      enum(success, failed, partial, stale)
  records_ingested:            int
  records_failed:              int
  failure_reason:              string | null
  retry_count:                 int
  last_successful_ingestion:   timestamp
  ingestion_gap_hours:         float             // hours since last successful ingestion
}
```

### 6.8 PerformanceSummary

A lightweight aggregated view used by the Dashboard Director for the Home page KPI section.

```
PerformanceSummary {
  channel_id:                  string
  computed_at:                 timestamp
  window_days:                 int               // default 30

  // Most recent episode
  latest_episode_id:           string | null
  latest_episode_published_at: timestamp | null
  latest_episode_ctr:          float | null
  latest_episode_performance_band: string | null

  // 30-day channel summary
  episodes_published_30d:      int
  episodes_target_30d:         int | null        // from publishing_goals.episodes_per_month
  avg_ctr_30d:                 float | null
  ctr_target:                  float | null      // from publishing_goals.ctr_target
  subscriber_count:            int | null
  subscriber_growth_30d:       int | null
  growth_target:               string | null     // from publishing_goals.growth_target

  // Active anomalies
  active_anomaly_count:        int
  active_anomaly_types:        string[]

  // Goal status snapshot
  goals_on_track:              int
  goals_at_risk:               int
  goals_off_track:             int
  goals_exceeded:              int
}
```

---

## 7. API Contracts

### 7.1 ingest_platform_analytics()

Called by: Analytics Layer ingestion pipeline (internal, batch).

```
ingest_platform_analytics(
  platform,
  channel_id,
  episode_id,
  analytics_period_start,
  analytics_period_end,
  raw_metrics             // platform-specific raw metric payload
) → { analytics_id, status, anomalies_detected }
```

Idempotent on `(platform, episode_id, analytics_period_start)`.

### 7.2 ingest_production_metrics()

Called by: Run OS and Cost Ledger (on run completion).

```
ingest_production_metrics(
  episode_id,
  run_id,
  channel_id,
  run_record              // structured run record from Run OS
) → { analytics_id }
```

### 7.3 get_episode_analytics()

Called by: Dashboard, Learning Module.

```
get_episode_analytics(
  episode_id,
  platform              // optional — returns all platforms if null
) → EpisodeAnalytics[]
```

### 7.4 get_channel_analytics()

Called by: Dashboard, Learning Module, Dashboard Director (for Home summary).

```
get_channel_analytics(
  channel_id,
  platform,             // optional
  period_type,          // daily | weekly | monthly
  date_from,
  date_to
) → ChannelAnalytics[]
```

### 7.5 get_production_analytics()

Called by: Dashboard, Learning Module.

```
get_production_analytics(
  channel_id,
  period_type,
  date_from,
  date_to
) → ChannelProductionAnalytics
```

### 7.6 get_goal_tracking()

Called by: Dashboard, OIS (for goal drift detection), Dashboard Director.

```
get_goal_tracking(
  channel_id
) → GoalTracking[]
```

### 7.7 get_performance_summary()

Called by: Dashboard Director (for Home page).

```
get_performance_summary(
  channel_id,
  window_days           // default 30
) → PerformanceSummary
```

### 7.8 get_ingestion_health()

Called by: Dashboard (Learning Confidence panel), OIS (for stale ingestion detection).

```
get_ingestion_health(
  channel_id,
  platform,             // optional
  date_from,
  date_to
) → IngestionHealthRecord[]
```

### 7.9 submit_analytics_to_learning()

Called by: Analytics Layer (internal, after each batch ingestion and anomaly detection cycle).

```
submit_analytics_to_learning(
  channel_id,
  episode_id,
  signal_type,          // maps to Learning Module domain
  metric_data,
  sample_size,
  measured_at
) → { signal_id, accepted: boolean }
```

Calls the Learning Module's `submit_learning_signal()` API.

### 7.10 get_episode_comparison()

Called by: Dashboard (episode performance comparison view).

```
get_episode_comparison(
  channel_id,
  episode_ids,          // list of episode_ids to compare side by side
  platform
) → EpisodeAnalytics[]  // ordered per input episode_ids
```

### 7.11 get_channel_benchmarks()

Called by: Dashboard, Learning Module.

```
get_channel_benchmarks(
  channel_id,
  metric,               // e.g., "ctr", "avg_view_percentage"
  window_days           // default 30
) → {
  metric,
  channel_average,
  channel_median,
  channel_p25,
  channel_p75,
  channel_best,
  channel_worst,
  window_days,
  episode_count
}
```

---

## 8. Dashboard Visibility

Analytics data is surfaced across multiple dashboard pages rather than on a dedicated Analytics page. Analytics is a view into performance, not a standalone operational tool.

### 8.1 Home page — KPI strip

A compact KPI strip shown at the top of the Home page (below the Pending Actions section). Populated from `PerformanceSummary`.

```
PERFORMANCE  [Channel: The Hypogeum Chronicles]  [Last 30 days]

  Episodes published:   2 / 4 target       ● at risk
  Avg CTR:             5.8%  (target: 6%)   ● on track
  Subscribers:         3,240  (+180 this month)  ● on track
  Latest episode:      ep_003  ·  5.2% CTR  ·  62% avg retention  ·  on_baseline

  [→ Full analytics on Channel page]
```

Goal status indicators use the same color scheme as status badges (PES C8). `at_risk` shows WARNING color, `off_track` shows CRITICAL color, `on_track` shows success color, `exceeded` shows success color with a ★.

### 8.2 Production page — per-episode cost and performance

Each episode in the production history table shows:
- Total production cost
- Gate pass summary (passed / failed / warnings)
- Pipeline success indicator
- Post-publish performance band (ctr, retention) once analytics stabilize

```
PRODUCTION HISTORY

ep_003  The Antikythera Mechanism  completed  $14.20  ✓ all gates
        Published 2026-06-08  ·  CTR: 5.2% (on_baseline)  ·  Retention: 62% (above_average)

ep_002  The Roman Forum            completed  $12.80  ✓ all gates
        Published 2026-06-01  ·  CTR: 6.1% (above_average)  ·  Retention: 58% (on_baseline)

ep_001  The Oracle at Delphi       failed     $3.20   ✗ VPD error — not reached gates
        Not published
```

### 8.3 Channel detail — analytics tab

The Channel detail page includes an Analytics tab showing:

**Episode performance table:**
- Episode title, published date, platform
- CTR with performance band indicator
- Avg retention with band indicator
- Views, watch time
- Revenue (if monetized)
- Sortable by any column

**Channel-level charts (displayed as sparklines or simple bar charts, not full charting widgets):**
- CTR trend (last 12 episodes)
- Avg retention trend (last 12 episodes)
- Subscriber growth (monthly, last 6 months)
- Cost per episode trend (last 12 episodes)

**Goal status panel:**
- Each active goal listed with current value, target, trajectory, and status badge
- "Not enough data" goals shown in muted style with guidance on when data will be available

**Production health panel:**
- Gate pass rates by type (BGA, QA, compliance) as percentages over 30 days
- Pipeline success rate
- Avg cost per episode (30-day rolling)
- Budget utilization

### 8.4 Analytics depth and interaction

The dashboard does not include interactive charting, drill-down filtering, or custom date range selectors in V1. The analytics surface is read-only and pre-computed. All values are computed by the Analytics Layer and stored; the dashboard displays stored values, not live queries.

The following interactions are available:
- [View episode] — navigates to the episode's production record
- [View gate verdicts] — navigates to the Review page gate verdict detail for this episode
- [Refresh] — triggers a re-fetch of the current analytics records (not a re-ingestion — it reads the latest stored values)

Custom analytics queries, channel-vs-channel comparisons, and date range exploration are explicitly deferred to V2.

### 8.5 Analytics on the Learning page

The Learning Confidence panel (B5.7 §18.4) includes ingestion health status from the Analytics Layer. This is the only overlap — the Learning page shows analytics health but not performance data.

### 8.6 Role-based visibility

| Section | Editor | Operator | Admin | Owner |
|---|---|---|---|---|
| Home KPI strip | ✓ | ✓ | ✓ | ✓ |
| Production page analytics | ✓ | ✓ | ✓ | ✓ |
| Channel analytics tab | ✓ | ✓ | ✓ | ✓ |
| Goal tracking | ✓ (read only) | ✓ | ✓ | ✓ |
| Revenue data | — | ✓ | ✓ | ✓ |
| Production cost data | — | ✓ | ✓ | ✓ |
| Ingestion health | — | ✓ | ✓ | ✓ |

Revenue and cost data are hidden from Editor-role users. Performance metrics (CTR, retention) are visible to all roles.

---

## 9. Integration Points

### 9.1 Analytics Layer → Learning Module

After each batch ingestion cycle, the Analytics Layer submits performance signals to the Learning Module via `submit_learning_signal()`. Signals are submitted once per episode per platform per ingestion cycle — not on every metric update.

What gets submitted to Learning:
- CTR signal (if episode is stabilized and performance band is established)
- Retention signal (if episode is stabilized and retention_curve is complete)
- Subscriber growth signal (after monthly rollup)
- Engagement signal (engagement_rate, where available)
- Production cost signal (on run completion)
- Gate failure cluster signal (when gate_failure_cluster anomaly is detected)

What does NOT get submitted to Learning:
- Raw counts (views, impressions) without derived metrics — Learning works with rates and trends, not raw counts
- Signals for episodes still in `pending_stabilization` status
- Duplicate signals for the same episode/platform/metric within 24 hours

### 9.2 Analytics Layer → OIS

The Analytics Layer submits `analytics_signal` records to OIS when anomalies are detected or goal status changes. OIS decides how to route each signal (alert, notification, suppressed noise).

The Analytics Layer does not route anomalies directly to operators. OIS routing rules determine whether an analytics anomaly becomes a dashboard notification, a Home alert, or a Learning page passive insight.

### 9.3 OIS → Analytics Layer

OIS does not send signals back to the Analytics Layer. The relationship is one-directional: Analytics Layer → OIS.

### 9.4 Dashboard Director → Analytics Layer

The Dashboard Director calls `get_performance_summary()` to populate the Home page KPI strip. The Dashboard Director does not query the full analytics store — it uses the pre-computed PerformanceSummary only.

### 9.5 Analytics Layer → Channel Profile

The Analytics Layer reads Channel Profile goals (`publishing_goals`, `monetization_goals`) to compute GoalTracking records. It does not write to the Channel Profile. If goals change (operator updates Channel Profile), the next goal tracking computation uses the new targets.

### 9.6 Analytics Layer and Platform API credentials

Platform API credentials (YouTube Data API key, TikTok API credentials, etc.) are stored in the Credential Manager (same system as all other provider credentials). The Analytics Layer reads credentials from the Credential Manager at ingestion time — it does not cache them.

If a credential expires or is revoked, the Analytics Layer ingestion fails for that platform. The Credential Manager emits a `credential_failure` signal to OIS independently. The Analytics Layer also emits an `ingestion_stale` signal. The operator receives notification from both paths.

---

## 10. Risk Register

### Risk 1 — Stabilization window not enforced

**Failure mode:** An episode is published Thursday. YouTube analytics are ingested Friday (18 hours later). CTR is anomalously high because the YouTube algorithm is still showing the episode broadly in the first 24 hours. The CTR signal is submitted to the Learning Module. A thumbnail recommendation is generated based on inflated CTR data.

**Mitigation:** Stabilization window (48h for YouTube) is enforced at ingestion time. Episodes with `analytics_period_end` < `published_at + 48h` are set to `status: pending_stabilization`. Pending stabilization records are excluded from all Learning Module signal submission and goal tracking. The Analytics Layer only submits a CTR signal after the stabilization window has elapsed.

---

### Risk 2 — Goal tracking against stale targets

**Failure mode:** An operator updates `publishing_goals.episodes_per_month` from 4 to 6 after achieving 4 episodes. The goal tracking system still evaluates against the old target for the next 30 days and reports `exceeded` when the operator expects `on_track`.

**Mitigation:** Goal tracking reads Channel Profile goals at evaluation time, not at ingestion time. Every GoalTracking record is computed fresh on each evaluation cycle. If the Channel Profile goal changes, the next evaluation cycle uses the new target. This means goal status may flip abruptly when goals are updated — this is correct behavior and is shown on the dashboard with the effective date of the current target.

---

### Risk 3 — Cross-platform metric comparison confusion

**Failure mode:** CTR on YouTube is calculated as views/impressions. CTR on TikTok is not a standard metric. The operator sees both labeled as "CTR" on the dashboard and compares them directly, drawing incorrect conclusions.

**Mitigation:** All metrics are labeled with the platform that produced them. Cross-platform comparison views show platform labels prominently. Platform-specific metric names and definitions are shown in tooltips. Where a metric cannot be computed for a platform (e.g., TikTok has no standard CTR), the field is `null` and shown as "N/A (not available on this platform)" rather than 0 or missing.

---

### Risk 4 — Analytics data used without minimum sample

**Failure mode:** The channel has published 2 episodes. Channel benchmarks are computed from 2 data points. Performance bands are applied to the third episode based on this tiny baseline. A retention drop on the third episode triggers an anomaly signal to OIS, which routes it to the Learning Module. The Learning Module generates a recommendation from 3 episodes of data.

**Mitigation:** Channel benchmarks require minimum 5 episodes before performance bands are computed. Before 5 episodes, all relative metrics and performance bands are `null`. Anomaly detection for relative metrics (relative_ctr, relative_retention) is disabled until the channel baseline exists. Learning Module's own minimum sample thresholds (§3 of B5.7) provide a second layer — even if an analytics signal is submitted, the Learning Module requires its own evidence threshold before generating a recommendation.

---

### Risk 5 — Ingestion failure silently degrades analytics quality

**Failure mode:** TikTok Analytics ingestion fails for 3 days. The operator does not notice. The Learning Module has no TikTok data for those days. Platform performance analytics for the affected episodes are incomplete. A publishing_recommendation is generated based only on YouTube data, incorrectly generalizing to TikTok timing.

**Mitigation:** Ingestion failures are tracked in IngestionHealthRecord and surfaced in the Learning Confidence panel. The Analytics Layer emits `ingestion_stale` signals to OIS after 36 hours without successful ingestion. GoalTracking and Learning Module signals for episodes missing one or more platforms are flagged `platforms_pending`. Learning Module signals include the source platform in their metadata — the Learning Module (and any human reviewing evidence) can see whether a recommendation is based on multi-platform or single-platform data.

---

### Risk 6 — Revenue data leaks to unauthorized roles

**Failure mode:** An Editor-role user opens the Channel analytics tab and sees revenue-per-episode data. Revenue data may be commercially sensitive.

**Mitigation:** Revenue fields in all analytics records are only returned by the API when the requesting user has Operator+ role. The API layer enforces this — it does not rely on the frontend to hide revenue fields. Editor-role users receive analytics records with `revenue_usd: null` and a `revenue_visible: false` flag. The dashboard uses this flag to suppress revenue display rather than showing zero or blank.

---

### Risk 7 — Analytics distorts Learning Module output

**Failure mode:** A high-performing episode drives up the channel CTR average. Subsequent episodes are measured against this higher baseline. They appear to underperform relative to the inflated average. The Learning Module receives repeated underperformance signals. Recommendations emerge to fix a "problem" that is actually reversion to the channel's true mean after an exceptional episode.

**Mitigation:** Channel benchmarks use rolling 30-day averages excluding statistical outliers (episodes in the top 5% or bottom 5% of the CTR distribution are excluded from the rolling average computation). The `relative_ctr` metric therefore measures against a robust central tendency, not a mean distorted by outliers. The Learning Module also requires minimum sample sizes and consistency scores — a pattern driven by one outlier episode is unlikely to produce a high-consistency signal.

---

## 11. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5 — Dashboard Architecture | Analytics data is displayed on the Home page KPI strip, Production page episode history, and Channel detail analytics tab. No dedicated Analytics page — analytics is embedded in the operational context. |
| B5.25 — PES | Analytics display follows PES C7 (empty states for insufficient data), C8 (status badges for goal tracking), and the progressive disclosure principle (summary by default, detail on demand). |
| B5.275 — OIS | Analytics Layer submits `analytics_signal` to OIS for all anomalies and goal drift events. OIS routes; Analytics does not. |
| B5.35 — Notification System | Analytics Layer does not call NS directly. Anomaly routing via OIS may produce NS notifications (e.g., `system_alert` for a severe pipeline failure rate). |
| B5.7 — Learning Module | Analytics Layer is the primary external signal source for the Learning Module. All platform performance signals are submitted via `submit_learning_signal()`. Signal submission follows the Learning Module's minimum sample and stabilization requirements. |
| B4.75 — Channel Profile | Analytics Layer reads `publishing_goals` and `monetization_goals` for goal tracking. If goals are absent (new channel, no goals configured), goal tracking returns `not_enough_data` for all goal fields. |
| B7 — Run OS | Run OS writes production metrics to the Analytics Layer via `ingest_production_metrics()` on run completion. The Analytics Layer stores these as ProductionAnalytics records. |
| Credential Manager | Platform API credentials are read from Credential Manager at ingestion time. Credential failures surface as ingestion failures. |
