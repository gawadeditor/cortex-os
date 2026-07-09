# Budget & Cost Governance Design

**Phase:** B5.11  
**Depends on:** B5.275 (OIS), B5.35 (Notification System), B5.4 (Approval System), B5.7 (Learning Module), B5.8 (Analytics Layer), B4.75 (Channel Profile), B4.9 (Engineering Director), B7 (Run OS)  
**Feeds into:** Runtime Execution, Provider/Model Director, OIS (budget signals), Approval System, Dashboard

---

## 1. Purpose

The Budget & Cost Governance System is the financial governance layer of Factory OS. It controls how much the factory spends, on what, and when production must stop to prevent runaway spending.

```
Analytics Layer         →  What did performance cost to achieve?
Learning Module         →  What cost optimizations might improve efficiency?
Budget Governance       →  Can the factory afford to run? May it proceed?
Approval System         →  May a budget exception proceed?
Execution Systems       →  Consume budget decisions; do not own them.
```

Budget Governance is the single authority on whether the factory has financial clearance to operate. No execution system makes its own spending decision — it checks Budget Governance first.

### 1.1 What Budget Governance does not own

| Concern | Owner |
|---|---|
| Deciding which model to use | Provider/Model Director (within budget constraints) |
| Generating cost-reduction recommendations | Learning Module |
| Approving budget exceptions | Approval System |
| Delivering budget notifications | Notification System (via OIS routing) |
| Recording run costs to ledger | Run OS |

Budget Governance reads the Cost Ledger, computes status, emits signals, enforces limits, and requests approvals. It does not generate content, execute runs, or decide which agents to use.

---

## 2. Budget Domains

Eight budget domains define the complete financial governance surface. Each domain is independently configurable, independently tracked, and independently enforced.

### 2.1 factory_budget

**Owner:** Owner / Admin  
**Scope:** Total spend across all channels and all operations within the factory, per calendar month.  
**Allocation rules:** Set by the Owner in Factory Settings. May not be exceeded by any combination of channel budgets (the factory budget is the ceiling that all channel budgets must sum below).  
**Rollover behavior:** No rollover. Each calendar month begins with a fresh allocation. Unused budget in one month does not carry forward.  
**Enforcement:** Hard limit by default. Reaching the factory budget ceiling blocks all new runs across all channels and emits a BLOCKED signal to OIS.

### 2.2 channel_budget

**Owner:** Operator / Admin  
**Scope:** Monthly spend for a specific channel, across all run types (generation, publishing, analytics).  
**Allocation rules:** Set per channel by the Operator. Sum of all channel budgets must not exceed the factory_budget. If a new channel budget would breach the factory ceiling, the allocation is rejected with an error.  
**Rollover behavior:** No rollover. Reset at start of each calendar month.  
**Enforcement:** Configurable per channel: informational, soft limit, or hard limit. Default: hard limit. Hard limit blocks new runs for that channel when the channel budget is exhausted, even if factory budget remains.

### 2.3 client_budget

**Owner:** Admin (factory side), on behalf of the client  
**Scope:** Monthly spend for a specific client's channels within the factory. A client may own multiple channels; the client_budget governs the aggregate.  
**Allocation rules:** Set by Admin per client. May not exceed the aggregate of the channel budgets assigned to the client's channels.  
**Rollover behavior:** No rollover.  
**Enforcement:** Hard limit. Client budget exhaustion blocks all new runs for the client's channels. The client never sees factory-internal cost details — they see their own usage summary only.

### 2.4 run_budget

**Owner:** System (derived from channel_budget / episode configuration)  
**Scope:** Maximum spend for a single run execution (one episode production pipeline).  
**Allocation rules:** Derived as `channel_budget.episode_ceiling_usd` from the Channel Profile, or from a per-run override. Not independently configurable beyond what the Channel Profile defines.  
**Rollover behavior:** Not applicable — single-run scope.  
**Enforcement:** Hard limit. If a run's cumulative cost is projected to significantly exceed the run_budget (by >25%), the run coordinator receives a warning. If the run's actual cost at a task boundary exceeds the run_budget, the run is halted at the next natural task boundary (not mid-task). In-progress tasks are not killed.

### 2.5 episode_budget

**Owner:** System (derived from channel budget)  
**Scope:** Maximum spend per completed episode, across all assets and all platforms it is published to.  
**Allocation rules:** Equal to `run_budget` by default. If an episode is produced across multiple runs (e.g., a re-run after rejection), the episode_budget covers the aggregate cost of all runs for that episode.  
**Rollover behavior:** Not applicable — single-episode scope.  
**Enforcement:** Informational tracking by default. Episode budget overruns are flagged in the episode cost record and surfaced in the dashboard but do not block production (the run has already completed).

### 2.6 provider_budget

**Owner:** Admin  
**Scope:** Maximum monthly spend with a specific external provider (OpenRouter, ElevenLabs, MUAPI, etc.).  
**Allocation rules:** Set per provider in Factory Settings. Optional — if not set, no per-provider ceiling applies.  
**Rollover behavior:** No rollover.  
**Enforcement:** Soft limit by default. Warning at 80%. When the provider budget ceiling is reached, Budget Governance signals the Provider/Model Director to route new tasks away from the provider if an equivalent alternative exists. If no alternative exists, the behavior follows the enforcement mode configured for this provider (default: informational — the run proceeds with an alert).

### 2.7 model_budget

**Owner:** Admin  
**Scope:** Maximum monthly spend on a specific model (e.g., Claude Opus, Seedance 1.0).  
**Allocation rules:** Set per model in Factory Settings. Optional.  
**Rollover behavior:** No rollover.  
**Enforcement:** Same as provider_budget — soft limit by default. When reached, signals PMD to route to an equivalent model within the same provider or across providers.

### 2.8 platform_budget

**Owner:** Admin  
**Scope:** Maximum monthly spend on a specific publishing platform's API costs (rare — most platforms do not charge per upload, but some analytics and API costs exist).  
**Allocation rules:** Set per platform. Optional — most operators do not set this in V1.  
**Rollover behavior:** No rollover.  
**Enforcement:** Informational by default. Platform API costs are typically small relative to AI generation costs.

### 2.9 Domain hierarchy

```
factory_budget (ceiling for all)
  └── channel_budget (per channel)
        └── client_budget (per client, spans multiple channels)
              ├── run_budget (per execution)
              │     └── episode_budget (per completed episode)
              ├── provider_budget (per external provider, cross-channel)
              ├── model_budget (per model, cross-channel)
              └── platform_budget (per publishing platform, cross-channel)
```

Provider, model, and platform budgets are cross-channel — they track spend across all channels that use that provider, model, or platform.

---

## 3. Cost Categories

Ten cost categories cover all spending in the factory. Every cost event is assigned to exactly one category.

| Category | What it covers | Typical unit |
|---|---|---|
| `llm_cost` | Input and output tokens for all LLM calls (Claude, GPT, etc.) | $/1M tokens |
| `image_generation_cost` | Image generation API calls (MUAPI, Stability, Replicate) | $/image |
| `video_generation_cost` | Video generation API calls (Seedance via MUAPI, Runway) | $/second of video |
| `voice_generation_cost` | Text-to-speech synthesis (ElevenLabs) | $/1K characters |
| `storage_cost` | Object storage for all produced assets (raw, intermediate, final) | $/GB/month |
| `publishing_cost` | API costs for publishing to platforms (rare; most platforms are free per upload) | $/operation |
| `analytics_cost` | API costs for fetching platform analytics (usually free tier; data egress costs) | $/operation |
| `external_api_cost` | Research and web access APIs (Perplexity, Brave Search, etc.) | $/query |
| `infrastructure_cost` | Compute, bandwidth, and hosting costs for the factory's own infrastructure | $/unit |
| `notification_cost` | Email delivery costs (if using a paid SMTP relay) | $/email |

### 3.1 Aggregation rules

Costs aggregate up the domain hierarchy:

```
category_cost (per task)
  → task_total (per task record)
    → stage_total (per stage, all tasks in stage)
      → run_total (per run, all stages)
        → episode_total (per episode, all runs for that episode)
          → channel_monthly_total (per channel, all episodes in month)
            → factory_monthly_total (all channels combined)
```

Provider budget aggregation is cross-channel: `sum of all task costs where task.provider = provider_id`.

Model budget aggregation: `sum of all task costs where task.model_id = model_id`.

All aggregations are computed from the Cost Ledger — the ground truth. Dashboard displays are computed from the Cost Ledger, not from cached summaries (to ensure accuracy).

---

## 4. Budget States

### 4.1 States

| State | Meaning | Color | Threshold |
|---|---|---|---|
| `healthy` | Spend is within normal operational range | Green | < 70% of ceiling |
| `warning` | Approaching the ceiling; monitoring required | Amber | 70%–84% of ceiling |
| `at_risk` | High probability of ceiling breach before period end | Amber/Red | 85%–94% of ceiling |
| `exhausted` | Ceiling has been reached; enforcement behavior applies | Red | ≥ 95% of ceiling |
| `blocked` | Production is halted by Budget Governance | Red with halt icon | 100% of ceiling (hard limit) |

The distinction between `exhausted` and `blocked`:
- `exhausted` applies when the budget is consumed. If the enforcement mode is soft or informational, the factory continues running even when exhausted (with escalating alerts).
- `blocked` applies only when enforcement mode is hard limit AND the budget is exhausted. Hard limit + exhausted = blocked.

### 4.2 State transitions

```
healthy
  └─→ warning         (spend crosses 70% threshold)

warning
  ├─→ healthy         (budget reset at period start, or budget increased)
  └─→ at_risk         (spend crosses 85% threshold)

at_risk
  ├─→ warning         (budget reset or increase)
  └─→ exhausted       (spend crosses 95% threshold)

exhausted
  ├─→ at_risk         (budget reset or increase)
  └─→ blocked         (hard limit enforcement AND spend ≥ 100%)

blocked
  ├─→ exhausted       (hard limit removed — budget still consumed but no longer blocking)
  └─→ healthy         (budget reset at period start, or override approved)
```

State transitions trigger `budget_signal` submissions to OIS.

### 4.3 Operator visibility requirements

| State | Home page alert | Notification | Run start blocked |
|---|---|---|---|
| `healthy` | None | None | No |
| `warning` | Informational card (low priority) | Digest only | No |
| `at_risk` | WARNING card in Pending Actions | Email if configured | No |
| `exhausted` (soft/info) | CRITICAL card | Immediate email + Telegram | No |
| `exhausted` (hard) | BLOCKED card | Immediate all channels | Yes |
| `blocked` | BLOCKED persistent banner | Immediate all channels | Yes |

The factory-level budget page shows the real-time state for all domains simultaneously so the operator has a unified financial view.

---

## 5. Budget Enforcement

### 5.1 Enforcement modes

Three enforcement modes are configurable per budget domain (except factory_budget, which is always hard limit).

**Informational only:**
- Budget state transitions emit signals to OIS and appear on the dashboard
- No execution is blocked at any budget state
- Used for: platform_budget, analytics_cost (low-impact spending with no production risk)

**Soft limit:**
- Budget state `warning` and `at_risk`: signals only, no blocking
- Budget state `exhausted`: operator receives CRITICAL alert; runs may proceed with an explicit per-run override acknowledgement
- Budget state `blocked` (>100%): not applicable to soft limit — soft limit never blocks
- Used for: provider_budget, model_budget, and channel_budget when the operator wants visibility without hard stops

**Hard limit:**
- Budget state `exhausted` (≥ 95%): CRITICAL alert; new runs enter a pending state until operator acknowledges
- Budget state `blocked` (= 100%): new runs cannot start; in-progress runs continue to completion; budget must be increased or the period must reset before new runs can begin
- Used for: factory_budget (always), channel_budget (default), client_budget (always)

### 5.2 Enforcement behavior per execution type

| Execution type | Hard limit behavior at `blocked` |
|---|---|
| New episode production run | Blocked from starting. `create_run()` returns `BUDGET_BLOCKED`. |
| In-progress production run | Continues to completion. Killing a mid-run wastes partial spend without producing value. |
| Re-run after rejection | Blocked from starting until budget is available. |
| Publishing run | Not blocked. An episode that has already been produced should be publishable even if the budget is exhausted. Publishing typically incurs minimal additional cost. |
| Analytics collection | Not blocked. Analytics collection is informational and typically low-cost. |
| Learning recommendations | Not blocked. Generating recommendations does not incur provider API costs. |
| Archive run | Suspended. Archive operations are non-critical and can wait. |

### 5.3 Run budget enforcement mid-execution

The run_budget is enforced at task boundaries, not mid-task:

1. Before each task starts, the run coordinator checks: `(run_cumulative_cost + task_estimated_cost) > run_budget × 1.25`
2. If the check fails (25% overrun tolerance), the task is not started and the run is halted
3. The completed tasks' outputs are preserved; the run is marked `halted_budget_exceeded`
4. A `budget_signal` is submitted to OIS with the halted run context
5. The operator reviews and may either: increase the run_budget for a re-run, or reject the episode

The 25% overrun tolerance exists because cost estimates are not exact — the actual task cost may be up to 25% above estimate without triggering a halt. Above that, the halt fires.

---

## 6. Cost Tracking Model

### 6.1 Cost tracking levels

| Level | Granularity | Primary use |
|---|---|---|
| Per task | Single agent invocation | Debugging, agent cost attribution |
| Per stage | Aggregate of tasks within a pipeline stage | Stage-level cost analysis |
| Per run | Total cost for one episode pipeline execution | Run cost management |
| Per episode | Total cost across all runs for one episode (including re-runs) | True episode cost |
| Per channel / monthly | Sum of all episode costs for a channel in a calendar month | Channel budget tracking |
| Per provider / monthly | Sum of all tasks using a provider in a calendar month | Provider budget tracking |
| Per model / monthly | Sum of all tasks using a model in a calendar month | Model budget tracking |
| Per factory / monthly | Sum of all channel costs in a calendar month | Factory budget tracking |

### 6.2 Cost fields tracked per cost event

Every cost event (task, API call, storage operation) is recorded with:

```
CostLedgerEntry {
  entry_id:             string
  factory_id:           string
  channel_id:           string
  run_id:               string | null
  episode_id:           string | null
  task_id:              string | null
  cost_category:        enum(llm_cost, image_generation_cost, video_generation_cost,
                              voice_generation_cost, storage_cost, publishing_cost,
                              analytics_cost, external_api_cost, infrastructure_cost,
                              notification_cost)
  provider_id:          string
  model_id:             string | null
  estimated_cost_usd:   float
  actual_cost_usd:      float            // filled after completion
  variance_usd:         float            // actual - estimated
  units_consumed:       float            // tokens, images, seconds, GB, etc.
  unit_type:            string           // "token", "image", "second", "GB", "query"
  incurred_at:          timestamp
  recorded_at:          timestamp
}
```

### 6.3 Budget tracking fields per domain

For each BudgetRecord the system maintains:

```
cumulative_cost_mtd:    float    // month-to-date actual spend
estimated_remaining:    float    // budget - cumulative_cost_mtd
forecast_cost_eom:      float    // projected total spend by end of month
variance_from_estimate: float    // actual vs. estimated cost trend
budget_utilization:     float    // cumulative_cost_mtd / budget_ceiling
```

### 6.4 Estimated vs. actual cost tracking

Cost estimates are generated before task execution (based on token count predictions, image resolution, video duration). Actual costs are recorded after. The variance between estimate and actual is tracked:

- If `variance_usd > 0.20 × estimated_cost_usd` (>20% overrun) for a single task, a cost anomaly signal is emitted
- Persistent positive variance (actual consistently higher than estimated) triggers a recalibration of the estimation model for that agent and provider

---

## 7. Budget Approval Flow

Budget Governance integrates with the Approval System for budget exceptions. Three categories of budget decision require Approval System routing.

### 7.1 Approval categories

**Budget increase:**
- Operator requests an increase to a channel_budget, client_budget, or factory_budget ceiling
- Risk level: MEDIUM (channel/client) to HIGH (factory)
- Authority: Operator+ for channel increases up to 2× current ceiling; Admin+ for >2× or factory increases

**Budget override (temporary exception):**
- Operator requests permission to run one episode that would exceed a hard budget limit
- Risk level: HIGH
- Authority: Admin+
- What changes: a single-run budget exception is granted for one run_id; the general limit remains

**Spending exception (emergency):**
- A critical production need requires running above the factory ceiling
- Risk level: CRITICAL
- Authority: Owner
- What changes: the factory is allowed to run for the specific episode; the overspend is recorded

**Premium provider usage:**
- Using a significantly more expensive provider or model that would accelerate a budget domain to `at_risk`
- Risk level: MEDIUM
- Authority: Operator+

### 7.2 Budget approval record

When a budget approval is submitted to the Approval System, the approval panel (PES C6) includes:

- "What changes": specific budget increase amount or exception scope
- "Why requested": current budget state, specific blocking situation
- "Risk level": derived from the approval category above
- "Affected entities": channels affected by the budget change
- "Cost evidence": current spend vs. ceiling, forecast to end of month
- "Rollback plan": if the exception is approved and spend is still unacceptably high, how it will be reversed (next period, or budget re-reduced)

### 7.3 Emergency execution without prior approval

In production, it is sometimes necessary to override a budget block immediately (e.g., a time-sensitive episode must publish before an event). The system supports an emergency execution path:

1. Operator (Admin+) initiates emergency execution with a written justification
2. The run proceeds immediately
3. An approval record is created retroactively in the Approval System with `triggered_by = "emergency"`
4. The Owner is notified immediately of the emergency execution
5. The retroactive approval record is reviewed and formally approved or rejected by the Owner within 24 hours
6. If retroactively rejected: no action reverses the completed run, but the rejection is recorded and the operator's authority to use emergency execution is flagged for review

Emergency execution is not available for the factory_budget hard limit — only for channel and run budget limits.

---

## 8. Forecasting

### 8.1 Forecast types

**Projected monthly spend:**
`projected_spend_eom = cumulative_cost_mtd + (daily_average_spend × days_remaining_in_month)`

The `daily_average_spend` uses the past 7 days' actual spend rather than the period-to-date average, to weight recent activity more heavily. If the factory has been idle (no runs), the forecast uses the channel's publishing cadence and historical episode cost.

**Projected channel spend:**
Same as projected monthly spend, computed per channel independently.

**Projected provider spend:**
Computed per provider based on the current run queue, pending episodes, and historical provider cost per episode.

**Projected growth impact:**
When a Learning Module recommendation proposes increasing episode cadence, Budget Governance computes the cost impact: `additional_episodes × avg_episode_cost`. This is included in the recommendation's `estimated_impact.cost_delta_usd_monthly`.

**Projected budget exhaustion date:**
`exhaustion_date = today + (budget_remaining / daily_average_spend)`

If `exhaustion_date < end_of_month`, a `forecast_signal` is submitted to OIS with the exhaustion date.

### 8.2 Forecast confidence

| Forecast window | Confidence | Limitations |
|---|---|---|
| Next 7 days | HIGH | Based on recent actual spend; queue visibility |
| Next 14 days | MEDIUM | Depends on run schedule adherence |
| End of month | MEDIUM to LOW | Production schedule may change; provider price changes |
| Beyond current month | LOW | Too many variables; shown as advisory only |

All forecasts are labeled with their confidence level in the dashboard. Low-confidence forecasts include a note: "Based on current cadence. Accuracy decreases with longer windows."

### 8.3 Forecast limitations

- Forecasts are linear extrapolations. They do not account for scheduled model upgrades, provider price changes, or operator decisions to change episode cadence.
- Forecasts for new channels (fewer than 3 completed episodes) have very low accuracy — shown as advisory only.
- Forecasts do not include costs from runs currently in progress (these costs are still estimated, not actual).

---

## 9. Learning Module Integration

### 9.1 What Learning consumes from Budget Governance

The Learning Module's Cost Intelligence Agent reads from Budget Governance:
- Per-episode cost by category (from Episode Cost Ledger)
- Per-provider and per-model cost trends
- Budget utilization trends (channel budget: actual vs. ceiling over time)
- Budget exhaustion events and their production impact
- Cost efficiency metrics (§12)

These are the signal sources for the `cost_efficiency` and `workflow_efficiency` learning domains (B5.7 §3).

### 9.2 What Learning may propose

The Learning Module may propose via `budget_recommendation`:
- Switching to a lower-cost model for a specific agent (sent as a signal to PMD — B5.7 §4.6)
- Reducing retries for agents with high retry rates relative to quality improvement
- Batch scheduling episodes to reduce per-run overhead
- Identifying which cost categories have the highest per-view cost (cost_per_view efficiency)

### 9.3 What Learning may not change

The Learning Module has no write authority to:
- Budget ceilings (factory_budget, channel_budget, client_budget)
- Enforcement modes (informational / soft / hard)
- Budget approval thresholds
- Provider budget allocations

All such changes flow through: Learning Recommendation → OIS → Approval System → Budget Governance (write via `update_budget()`). The Learning Module submits a recommendation; it does not configure budgets.

---

## 10. Provider / Model Director Integration

### 10.1 Budget-aware provider selection

When the Provider/Model Director selects a provider or model for a task, it consults Budget Governance for the budget state of each candidate:

```
For each candidate provider/model:
  1. Check provider_budget.status for this provider
  2. Check model_budget.status for this model
  3. Check channel_budget remaining capacity vs. estimated task cost

If provider budget is exhausted (hard limit):
  → Exclude this provider from selection
  → Signal: "Provider X excluded from selection: provider_budget exhausted"

If provider budget is at_risk (soft limit):
  → Downweight this provider in selection scoring
  → Prefer equivalent-quality alternative with lower cost

If no affordable provider meets quality threshold:
  → Block task execution
  → Submit budget_signal to OIS
  → Return BUDGET_BLOCKED to run coordinator
```

### 10.2 Budget-quality tradeoff rule

PMD may never select a lower-quality provider solely to meet budget constraints when the quality impact would cause gate failures. The rule:

```
if expected_quality_delta < -0.15 due to budget-driven provider downgrade:
  → Do not downgrade
  → Block the task and surface to Budget Governance as a budget_exception_required
  → Operator must explicitly approve the lower-quality choice
```

A 15% or greater quality downgrade for budget reasons requires human approval (LOW risk approval in the Approval System). This prevents automated cost optimization from silently degrading output quality below gate thresholds.

### 10.3 PMD cost reporting to Budget Governance

After every provider/model selection decision, PMD reports to Budget Governance:
- The selected provider and model
- The estimated cost for this task
- Whether the selection was budget-influenced (i.e., the budget-optimal choice rather than the quality-optimal choice)

Budget-influenced selections are flagged in the cost ledger entry so operators can identify how often budget pressure affects quality choices.

---

## 11. Episode Cost Ledger

### 11.1 Purpose

The Episode Cost Ledger is the financial ground truth for every episode produced. Every cost incurred in producing and publishing an episode is recorded as a line item and aggregated into the episode's total cost record.

### 11.2 EpisodeCostRecord schema

```
EpisodeCostRecord {
  episode_id:               string
  channel_id:               string
  factory_id:               string

  // Run costs (one or more runs per episode)
  run_ids:                  string[]
  production_run_count:     int         // >1 if episode was re-run after rejection
  
  // Cost by category
  llm_cost_usd:             float
  image_generation_cost_usd: float
  video_generation_cost_usd: float
  voice_generation_cost_usd: float
  storage_cost_usd:         float
  publishing_cost_usd:      float
  analytics_cost_usd:       float
  external_api_cost_usd:    float
  infrastructure_cost_usd:  float
  notification_cost_usd:    float
  total_cost_usd:           float       // sum of all categories

  // Cost by provider (top providers by spend)
  cost_by_provider:         map<provider_id, float>

  // Cost by model
  cost_by_model:            map<model_id, float>

  // Cost by platform publishing
  cost_by_platform:         map<platform, float>

  // Budget compliance
  run_budget_ceiling:       float
  was_over_run_budget:      boolean
  budget_override_approved: boolean     // if run exceeded budget with approval

  // Estimates vs. actuals
  total_estimated_cost_usd: float
  total_actual_cost_usd:    float
  cost_estimate_variance:   float       // actual - estimated

  // Timing
  first_run_started_at:     timestamp
  final_run_completed_at:   timestamp
  published_at:             timestamp | null

  finalized_at:             timestamp
  finalized:                boolean     // false while run is in progress
}
```

### 11.3 Multi-run episode cost

When an episode requires multiple runs (rejection and re-run), all run costs accumulate in the same EpisodeCostRecord. The `production_run_count` tracks how many runs were needed. This is important for the `cost_per_successful_run` efficiency metric — multiple runs for one published episode indicate a process inefficiency.

---

## 12. Cost Efficiency Metrics

Cost efficiency metrics join episode cost with platform performance to answer "are we spending effectively?"

| Metric | Formula | Usage |
|---|---|---|
| `cost_per_episode` | `total_cost_usd` | Direct cost tracking |
| `cost_per_view` | `total_cost_usd / total_views` | ROI per viewer |
| `cost_per_retained_minute` | `total_cost_usd / (total_views × avg_view_duration_minutes)` | Content efficiency |
| `cost_per_subscriber` | `monthly_channel_cost / subscribers_gained_in_month` | Acquisition efficiency |
| `cost_per_publish` | `publishing_cost_usd / platforms_published_count` | Platform cost efficiency |
| `cost_per_successful_run` | `total_production_cost / episodes_published` | Pipeline efficiency (includes re-run costs) |

### 12.1 Usage rules

- `cost_per_view` and `cost_per_retained_minute` require analytics stabilization (48h for YouTube) before they can be computed.
- `cost_per_subscriber` is a monthly aggregate — not meaningful at the per-episode level.
- `cost_per_successful_run` highlights episodes that required multiple runs: an episode with 3 runs has 3× the cost of an episode with 1 run for the same output.
- These metrics are submitted to the Learning Module as performance signals (efficiency domain). Learning uses them to identify cost optimization opportunities.
- Efficiency metrics are not used to block execution — they are diagnostic. Budget states (§4) are what block execution.

---

## 13. Budget Readiness

Before each type of factory operation, Budget Governance performs a readiness check.

### 13.1 Readiness states

| State | Meaning |
|---|---|
| `ready` | Budget is healthy or at warning/at_risk with soft/info enforcement — operation may proceed |
| `warning` | Budget is at_risk — operation may proceed but warning is surfaced to the run record |
| `blocked` | Budget is exhausted with hard enforcement — operation cannot proceed |

### 13.2 Readiness checks per operation type

| Operation | Budget domains checked | Blocked state |
|---|---|---|
| Channel activation | channel_budget allocation exists; factory_budget has headroom | factory_budget blocked |
| New run (episode production) | factory_budget, channel_budget, run_budget headroom | Any hard-limit blocked domain |
| Task execution (mid-run) | run_budget remaining + provider_budget + model_budget | run_budget exceeded by >25%, or provider/model budget hard limit |
| Episode re-run | Same as new run; episode_budget accumulation tracked | Same as new run |
| Publishing run | factory_budget (informational only — publishing not blocked by budget) | None |
| Analytics collection | analytics_cost budget (informational) | None |
| Learning runs | No budget check — recommendation generation incurs no external API cost | None |

### 13.3 BudgetReadinessReport

```
BudgetReadinessReport {
  channel_id:           string
  checked_at:           timestamp
  overall_status:       enum(ready, warning, blocked)
  blocking_domains:     BudgetDomain[]
  warning_domains:      BudgetDomain[]
  factory_budget:       BudgetStatus
  channel_budget:       BudgetStatus
  run_budget_headroom:  float           // headroom for one more run at current avg episode cost
  provider_budgets:     map<provider_id, BudgetStatus>
  model_budgets:        map<model_id, BudgetStatus>
  estimated_episodes_remaining: int     // how many more episodes budget allows at current avg cost
  budget_reset_at:      timestamp       // next period reset
}
```

---

## 14. Integration with OIS

Budget Governance submits five signal types to OIS.

### 14.1 Signal types

| Signal type | Trigger | Default OIS routing |
|---|---|---|
| `budget_signal` | Budget domain state transitions (healthy → warning, warning → at_risk, etc.) | warning → digest; at_risk → dashboard alert; exhausted → immediate |
| `overspend_signal` | Actual cost exceeded estimate by >20% for a single task | Learning page (cost efficiency domain) |
| `forecast_signal` | Projected exhaustion date is < 14 days from now | Dashboard warning + digest |
| `exhaustion_signal` | Budget domain reaches `blocked` state | BLOCKED — all channels, bypasses quiet hours |
| `efficiency_signal` | Cost efficiency metric is significantly above/below channel baseline | Learning page (passive insight) |

### 14.2 OIS routing by severity

| Budget state | Signal severity | OIS classification |
|---|---|---|
| `warning` (70–84%) | WARNING | Scheduled review / digest |
| `at_risk` (85–94%) | WARNING to CRITICAL | Immediate action if close to block |
| `exhausted` (soft) | CRITICAL | Immediate notification |
| `blocked` (hard) | BLOCKED | Immediate all channels; production halted |

OIS applies its own deduplication windows. A `budget_signal` at warning level is not re-emitted more than once every 12 hours. An `exhaustion_signal` (BLOCKED) has no deduplication — it fires every time a new run attempt is blocked.

---

## 15. Integration with Approval System

Budget Governance submits budget approval requests to the Approval System via OIS routing. Three approval types:

| Approval type | Approval System type | Risk level | Authority |
|---|---|---|---|
| Budget increase | `budget_recommendation` (via Learning path) or direct `budget_override` | MEDIUM to HIGH | Operator+ (moderate increase) / Admin+ (large increase) |
| Single-run budget exception | `budget_recommendation` | HIGH | Admin+ |
| Emergency execution | Created retroactively | CRITICAL (retroactive) | Owner review within 24h |

Budget Governance provides the `RecommendationEvidence` equivalent (current spend, forecast, blocking context) as the evidence package for the Approval System's C6 panel.

After approval:
- Budget increase: `update_budget()` is called with the new ceiling
- Single-run exception: a `BudgetException` record is created allowing the specific run to proceed
- The Approval System signals Budget Governance via `record_budget_decision()`

---

## 16. Dashboard Visibility

### 16.1 Budget page layout

```
BUDGET & COSTS
──────────────────────────────────────────────────────────────────────

FACTORY OVERVIEW  [June 2026]
  Total spend:     $187.40 / $300.00   ████████████░░░  62%  ● healthy
  Forecast (EOM):  $241.00             (based on current run rate)
  Episodes so far: 8  ·  Avg cost:  $23.43/episode

CHANNELS
┌──────────────────────────────────────────────────────────────────┐
│  Hypogeum Chronicles   $187.40 / $300.00   62%   ● healthy      │
│  Avg $23.43/ep  ·  Forecast $241.00/mo  ·  4 runs remaining     │
│  [View breakdown]                                                │
└──────────────────────────────────────────────────────────────────┘

PROVIDER SPEND  (This Month)
┌──────────────────────────────────────────────────────────────────┐
│  OpenRouter / Claude    $98.20     52%  ● no limit set           │
│  MUAPI / Seedance       $72.10     38%  ● no limit set           │
│  ElevenLabs             $12.40      7%  ● no limit set           │
│  Perplexity             $4.70       3%  ● no limit set           │
└──────────────────────────────────────────────────────────────────┘

COST BY CATEGORY
  LLM:              $98.20   52%
  Video generation: $64.30   34%
  Voice:            $12.40    7%
  Image generation: $7.80     4%
  Storage:          $2.20     1%
  External APIs:    $4.70     3%   [View episode-level breakdown]

RECENT EPISODES
  ep_008  $21.80   [✓ on budget]
  ep_007  $22.40   [✓ on budget]
  ep_006  $28.10   [⚠ +22% vs avg — 2 re-runs]  [View detail]

PENDING BUDGET APPROVALS  (0)
──────────────────────────────────────────────────────────────────────
```

### 16.2 Episode cost breakdown (detail view)

```
EPISODE COST DETAIL — ep_006 / The Roman Forum

Total:    $28.10 (2 runs — rejected on first attempt)

Run 1 (ep_006_r1) — $14.90 — rejected (quality_insufficient)
  LLM (script + VPD + BGA):     $6.40
  Video generation (3 scenes):  $7.20
  Voice synthesis:               $1.10
  Storage:                       $0.20

Run 2 (ep_006_r2) — $13.20 — published
  LLM (script revision + BGA):  $5.80
  Video generation (2 new scenes): $6.10
  Voice synthesis:               $1.10
  Storage:                       $0.20

Cost efficiency:
  Cost per view:          $0.0028   (10,142 views)
  Cost per retained min:  $0.009
  Cost vs. channel avg:   +20%
```

### 16.3 Blocked execution visibility

When a channel is budget-blocked, the Production page shows:

```
⛔ BUDGET BLOCKED — Hypogeum Chronicles

  Monthly budget exhausted: $300.00 / $300.00 (100%)
  Budget resets: July 1, 2026 (in 16 days)

  2 episodes are queued but cannot start.

  Options:
    [Request budget increase →]   Approve a higher ceiling
    [Request run exception →]     Approve one episode to proceed
    [View cost report →]          Understand where budget was spent

  Or wait for budget reset on July 1.
```

### 16.4 Role-based budget visibility

| Section | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| Factory-level budget | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Channel budget & costs | ✓ | ✓ | ✓ (assigned channels) | ✗ | ✗ | ✗ |
| Client budget | ✓ | ✓ | ✗ | ✗ | ✗ | Own channels only (spend %, not USD) |
| Provider spend detail | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| Episode cost breakdown | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Forecasting | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Cost efficiency metrics | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Budget approval actions | ✓ | ✓ (up to their authority) | ✗ | ✗ | ✗ | ✗ |

Clients see a spend percentage for their own channel(s) — not absolute USD amounts. Internal provider costs are never shown to clients.

---

## 17. Data Contracts

### 17.1 BudgetRecord

```
BudgetRecord {
  budget_id:              string
  domain:                 enum(factory, channel, client, run, episode, provider, model, platform)
  entity_id:              string          // factory_id, channel_id, client_id, etc.
  factory_id:             string
  period_start:           date
  period_end:             date
  ceiling_usd:            float
  enforcement_mode:       enum(informational, soft_limit, hard_limit)
  status:                 enum(healthy, warning, at_risk, exhausted, blocked)
  cumulative_cost_mtd:    float
  forecast_cost_eom:      float
  budget_utilization:     float           // 0.0–1.0
  estimated_remaining:    float
  created_by:             string
  created_at:             timestamp
  updated_by:             string
  updated_at:             timestamp
}
```

### 17.2 BudgetAllocation

```
BudgetAllocation {
  allocation_id:          string
  factory_id:             string
  domain:                 string
  entity_id:              string
  ceiling_usd:            float
  previous_ceiling_usd:   float | null
  change_reason:          string
  approved_by:            string
  approval_id:            string | null   // Approval System record if required
  effective_from:         date
  created_at:             timestamp
}
```

### 17.3 BudgetStatus

```
BudgetStatus {
  domain:                 string
  entity_id:              string
  status:                 enum(healthy, warning, at_risk, exhausted, blocked)
  ceiling_usd:            float
  cumulative_cost_mtd:    float
  budget_utilization:     float
  forecast_cost_eom:      float
  days_until_exhaustion:  int | null
  budget_reset_at:        timestamp
  enforcement_mode:       string
  checked_at:             timestamp
}
```

### 17.4 CostLedgerEntry

Defined in §6.2.

### 17.5 EpisodeCostRecord

Defined in §11.2.

### 17.6 BudgetForecast

```
BudgetForecast {
  forecast_id:            string
  domain:                 string
  entity_id:              string
  forecast_window_days:   int
  forecast_date:          date            // what date this forecast was generated
  projected_spend_eom:    float
  projected_exhaustion_date: date | null  // null if not expected this period
  confidence:             enum(HIGH, MEDIUM, LOW)
  basis:                  string          // "7-day rolling average" | "episode cadence" | etc.
  limiting_factors:       string[]        // what could make the forecast inaccurate
  generated_at:           timestamp
}
```

### 17.7 BudgetApprovalRequest

```
BudgetApprovalRequest {
  request_id:             string
  approval_type:          enum(budget_increase, run_exception, emergency_execution,
                               premium_provider, spending_exception)
  factory_id:             string
  channel_id:             string | null
  current_ceiling_usd:    float
  requested_ceiling_usd:  float | null    // for budget_increase
  exception_run_id:       string | null   // for run_exception
  current_spend_usd:      float
  forecast_eom_usd:       float
  justification:          string
  approval_id:            string          // Approval System record
  status:                 enum(pending, approved, rejected, expired)
}
```

### 17.8 BudgetSignal

```
BudgetSignal {
  signal_id:              string
  signal_type:            enum(budget_signal, overspend_signal, forecast_signal,
                               exhaustion_signal, efficiency_signal)
  domain:                 string
  entity_id:              string
  factory_id:             string
  severity:               enum(INFO, WARNING, CRITICAL, BLOCKED)
  budget_utilization:     float
  current_spend_usd:      float
  ceiling_usd:            float
  days_until_exhaustion:  int | null
  production_blocked:     boolean
  emitted_at:             timestamp
}
```

### 17.9 BudgetReadinessReport

Defined in §13.3.

### 17.10 CostEfficiencyMetrics

```
CostEfficiencyMetrics {
  channel_id:             string
  computed_at:            timestamp
  window_days:            int

  cost_per_episode_avg:   float
  cost_per_episode_trend: enum(increasing, stable, decreasing)
  cost_per_view:          float | null      // null until analytics stable
  cost_per_retained_minute: float | null
  cost_per_subscriber:    float | null      // monthly aggregate only
  cost_per_publish:       float
  cost_per_successful_run: float            // includes re-run costs

  // Category breakdown as % of total
  llm_cost_pct:           float
  image_cost_pct:         float
  video_cost_pct:         float
  voice_cost_pct:         float
  other_cost_pct:         float

  // Benchmark comparison (vs. channel historical average)
  cost_vs_channel_avg:    float             // % above or below channel's own historical avg
}
```

---

## 18. API Contracts

### 18.1 create_budget()

Called by: Dashboard (Admin+, Owner).

```
create_budget(
  domain,
  entity_id,
  factory_id,
  ceiling_usd,
  enforcement_mode,
  effective_from
) → BudgetRecord
```

### 18.2 update_budget()

Called by: Dashboard (Admin+, Owner), Approval System (after budget increase approved).

```
update_budget(
  budget_id,
  ceiling_usd,
  enforcement_mode,
  change_reason,
  approval_id           // required if ceiling_usd change required approval
) → BudgetRecord
```

### 18.3 allocate_budget()

Called by: Dashboard (Admin+) when assigning channel budgets within factory ceiling.

```
allocate_budget(
  factory_id,
  channel_id,
  ceiling_usd,
  enforcement_mode
) → BudgetAllocation
```

Validates: `sum(all_channel_budgets + new_allocation) ≤ factory_budget.ceiling_usd`

### 18.4 record_cost()

Called by: Run OS, Publishing Pipeline, Analytics Layer, Infrastructure (for storage costs).

```
record_cost(
  factory_id,
  channel_id,
  run_id,
  episode_id,
  task_id,
  cost_category,
  provider_id,
  model_id,
  estimated_cost_usd,
  actual_cost_usd,
  units_consumed,
  unit_type,
  incurred_at
) → CostLedgerEntry
```

### 18.5 get_budget_status()

Called by: Dashboard, Run OS (pre-run check), PMD (provider selection).

```
get_budget_status(
  factory_id,
  domain,               // optional
  entity_id             // optional
) → BudgetStatus[]
```

### 18.6 get_budget_forecast()

Called by: Dashboard, Learning Module (for cost projections in recommendations).

```
get_budget_forecast(
  factory_id,
  domain,
  entity_id,
  forecast_window_days  // default 30
) → BudgetForecast
```

### 18.7 get_budget_readiness()

Called by: Run OS (before starting new run), Channel Activation gate.

```
get_budget_readiness(
  factory_id,
  channel_id
) → BudgetReadinessReport
```

### 18.8 request_budget_override()

Called by: Dashboard (Admin+, Owner).

```
request_budget_override(
  factory_id,
  channel_id,
  override_type,        // "run_exception" | "emergency_execution" | "budget_increase"
  justification,
  requested_run_id,     // for run_exception
  requested_ceiling_usd // for budget_increase
) → BudgetApprovalRequest
```

Internally: calls Approval System `create_approval()` and returns the linked approval record.

### 18.9 record_budget_decision()

Called by: Approval System (after budget approval decision).

```
record_budget_decision(
  request_id,
  decision,             // approved | rejected
  decided_by,
  decided_at,
  approval_id
) → BudgetRecord        // updated record if approved
```

### 18.10 get_cost_breakdown()

Called by: Dashboard, Analytics Layer, Learning Module.

```
get_cost_breakdown(
  factory_id,
  channel_id,           // optional
  period_start,
  period_end,
  breakdown_by          // "category" | "provider" | "model" | "episode" | "stage"
) → CostLedgerEntry[]
```

### 18.11 get_cost_efficiency_metrics()

Called by: Dashboard, Learning Module (cost_efficiency domain signals).

```
get_cost_efficiency_metrics(
  channel_id,
  window_days           // default 30
) → CostEfficiencyMetrics
```

---

## 19. Local-First and SaaS Readiness

### 19.1 Local-first V1

In V1, Budget Governance operates as a single-factory, single-tenant system. All budget records are stored locally. No cross-factory budget aggregation. Client budget tracking is optional and configured by the operator for multi-client factories.

Cost Ledger entries are stored locally alongside Run OS records. Budget state is computed in real-time from the Cost Ledger — no separate cache that could drift.

### 19.2 SaaS readiness

**Per-tenant isolation:** Every BudgetRecord, CostLedgerEntry, and EpisodeCostRecord is scoped to `factory_id`. No budget or cost data crosses tenant boundaries.

**Client budget isolation:** In a multi-tenant SaaS where one factory manages client channels, the client_budget domain provides spend isolation. A client overspend does not draw from another client's allocation.

**Audit trail:** Every budget change (create, update, allocation, exception) is logged with `approved_by`, `approval_id`, and the before/after values. The audit trail is immutable and per-factory.

**Billing integration (V2):** In SaaS, the Cost Ledger's `actual_cost_usd` aggregates form the basis for SaaS billing to the factory operator. The Cost Ledger must be the authoritative source — billing disputes are resolved against the Cost Ledger, not against provider invoices directly (though the two should reconcile).

---

## 20. Risk Register

### Risk 1 — Uncontrolled spend

**Failure mode:** The factory has a soft limit on channel_budget. An operator queues 20 episodes over a weekend. The factory runs all 20. Total spend: $460. Budget ceiling: $300. The operator sees the overspend on Monday morning.

**Mitigation:** Channel budget enforcement mode defaults to hard limit. To set a soft limit, Admin must explicitly change the enforcement mode. The default protects against the weekend-queue scenario. For operators who prefer soft limits, the soft limit still triggers CRITICAL alerts at 95% utilization — the operator is notified before the 20th episode runs. The forecast alert fires at 14 days before projected exhaustion, providing early warning.

---

### Risk 2 — Hidden provider costs

**Failure mode:** A model upgrade increases the per-token cost by 40%. The Budget Governance system tracks total spend but does not surface the per-model cost increase. The factory hits the budget ceiling two weeks early and the operator does not understand why.

**Mitigation:** Per-model cost tracking (model_budget domain) breaks out spend by model. The `cost_by_model` field in EpisodeCostRecord shows cost per model per episode. The Budget dashboard's provider/model breakdown shows spend trends. An `overspend_signal` fires when any model's cost per episode exceeds 20% above its 30-day average — surfacing the unexpected model cost increase to OIS before the budget is exhausted.

---

### Risk 3 — Quality optimization destroying profitability

**Failure mode:** The Learning Module recommends switching to a higher-quality (but more expensive) model. The recommendation is approved. Episode cost rises from $22 to $38 per episode. The channel's monthly budget hits the ceiling with 3 fewer episodes produced. Profitability declines.

**Mitigation:** The Learning Module's budget_recommendation must include a `cost_delta_usd_monthly` estimate in its impact evidence (B5.7 §4.10). The Approval System's C6 approval panel shows this estimate prominently. Budget Governance also runs a forecast when any model upgrade is approved — if the upgrade would push the channel_budget to `at_risk` within the current period, an automatic forecast_signal is submitted to OIS alongside the approval confirmation. The operator sees both the quality improvement estimate and the budget impact in the same decision flow.

---

### Risk 4 — Cost optimization destroying quality

**Failure mode:** PMD selects a lower-cost model for the script agent because the provider budget is at_risk. The script quality declines. The BGA gate failure rate rises. Episodes require re-runs, costing more than the original cheaper model would have saved.

**Mitigation:** The quality-budget tradeoff rule (§10.2) prohibits automatic model downgrade that would produce a >15% quality delta. Re-run cost is tracked in `production_run_count` and `cost_per_successful_run` — if re-run rates rise, the Learning Module's cost efficiency signals detect this pattern and the Cost Intelligence Agent can recommend reverting the downgrade. The PMD quality floor prevents the race-to-the-bottom scenario; the efficiency metrics detect if it still happens.

---

### Risk 5 — Inaccurate forecasts driving incorrect decisions

**Failure mode:** The forecast shows budget exhaustion in 3 days. The operator panics and requests a budget increase (approved). The forecast was wrong because of a data ingestion delay — actual spend was much lower. The operator ends the month with a $200 budget surplus.

**Mitigation:** Forecasts are labeled with their confidence level (§8.2) and the basis for the projection. Low-confidence forecasts include a note. The forecast is clearly distinguished from the actual spend on the dashboard — "Forecast: $241 (Medium confidence, based on 7-day average)" is shown alongside "Actual: $187." Budget approval requests include the current actual spend and the forecast separately, so the approver can see both. Operator is encouraged to verify the forecast basis before requesting emergency increases.

---

### Risk 6 — Approval bypass for budget exceptions

**Failure mode:** An operator discovers that `request_budget_override()` with `override_type = "emergency_execution"` bypasses approval. They use it for every run when they want to avoid the approval wait.

**Mitigation:** Emergency execution is logged and the Owner is notified immediately — every time, not just the first time. Emergency execution creates a retroactive approval record that the Owner must review within 24 hours (§7.3). The audit trail records every use of the emergency path. OIS tracks the frequency: if emergency execution is used more than twice in a calendar month for the same channel, a `security_warning` signal is emitted to the Owner. Repeated misuse is surfaced as an operator behavior pattern.

---

### Risk 7 — Budget exhaustion during production

**Failure mode:** A run starts with enough budget headroom. A provider cost spike (unusual token usage in a complex script) causes the run to exceed the run_budget mid-execution. The run is killed mid-way. The partial cost is wasted and the incomplete episode cannot be published.

**Mitigation:** Run budget enforcement at task boundaries (§5.3): the run is not killed mid-task. The 25% overrun tolerance absorbs moderate cost spikes without stopping the run. If the run must halt at a task boundary, the completed partial output is preserved (though not publishable without the remaining stages). The run is marked `halted_budget_exceeded` and the operator can choose to: extend the run_budget for a resumption, or start a fresh run. The partial outputs may be reusable in the resumed run (archive lookup).

---

### Risk 8 — Incorrect cost attribution

**Failure mode:** A shared ElevenLabs API call is used for both Channel A (Hypogeum) and a test run for Channel B. The cost is attributed entirely to Channel A because that was the `channel_id` on the task record.

**Mitigation:** Every cost ledger entry has a single `channel_id` that is set at run creation and cannot be changed. Shared infrastructure costs (storage for the factory, not a specific channel) are attributed to `infrastructure_cost` under the factory domain, not to a channel. Test runs must be explicitly tagged as belonging to a channel — a test run with no channel_id is not allowed. If infrastructure costs need to be apportioned across channels, they are done as an end-of-period allocation operation by the factory, not as an implicit attribution.

---

### Risk 9 — Client budget leakage

**Failure mode:** Client A's channel uses the factory's shared AI providers. The provider budget is a factory-wide budget, not client-specific. Client A's heavy usage drives the provider budget to `at_risk`, which causes PMD to downgrade the model for Client B's next run.

**Mitigation:** Provider and model budgets are factory-level governance. Client budget isolation (client_budget domain) controls the total spend a client incurs — not the providers they use. If a factory serves multiple clients and wants to prevent client A's usage from affecting client B, it should configure separate provider budget allocations per client channel or use client-specific credential profiles that point to separate provider accounts. In V1, complete per-client provider isolation requires operator configuration — it is not automatic.

---

### Risk 10 — SaaS tenant budget isolation failure

**Failure mode:** In multi-tenant SaaS, Tenant A's cost ledger entries are visible to Tenant B's budget dashboard due to a missing `factory_id` filter in a query.

**Mitigation:** Every Budget Governance API endpoint requires `factory_id` as a mandatory parameter and validates that the authenticated user's factory matches the requested `factory_id`. The query layer enforces `WHERE factory_id = ?` on all cost data queries. No query returns cost data across factory boundaries. Audit logging records every cross-factory access attempt (which would be an error) as a security_warning to the Owner. Budget data is never returned in aggregate across factories in any standard API response.

---

## 21. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.275 — OIS | Budget Governance submits budget_signal, overspend_signal, forecast_signal, exhaustion_signal, and efficiency_signal to OIS. OIS routes per its attention budget and classification rules. |
| B5.35 — Notification System | NS delivers budget_warning and budget_blocked notifications (routed via OIS). NS does not query Budget Governance directly — it receives notification payloads from OIS. |
| B5.4 — Approval System | Budget exceptions and increases route through Approval System. Budget Governance provides the cost evidence; Approval System provides the decision lifecycle. |
| B5.7 — Learning Module | Learning Module reads cost efficiency metrics via `get_cost_efficiency_metrics()`. Learning Module submits budget_recommendation proposals via OIS — it does not call `update_budget()` directly. |
| B5.8 — Analytics Layer | Analytics Layer reads episode cost records (from EpisodeCostRecord) to compute cost efficiency metrics (cost_per_view, cost_per_retained_minute). Analytics ingestion itself records its own API costs via `record_cost()`. |
| B4.75 — Channel Profile | Channel Profile's `episode_ceiling_usd` (Channel Budget layer) seeds the run_budget. `monetization_goals` inform cost efficiency targets. `learning_goals.track_cost` determines whether cost efficiency signals are submitted to the Learning Module. |
| B7 — Run OS | Run OS calls `record_cost()` after every task completion. Run OS calls `get_budget_readiness()` before starting a new run. Run OS halts runs at task boundaries when run_budget is exceeded. |
| Provider / Model Director | PMD calls `get_budget_status()` for all candidate providers/models before selection. PMD excludes providers with exhausted hard-limit budgets from selection. PMD reports budget-influenced selections to Budget Governance. |
