# Learning & Intelligence Layer — Design Document

**Phase:** B6.5  
**Status:** Design Only  
**Date:** 2026-06-13  
**Scope:** Factory OS intelligence layer — raw data to decisions. No implementation, no code, no database changes.

---

## 1. Learning Architecture

### Conceptual Flow

```
Factory
└── Runs                        (every episode pipeline execution, every agent run)
    └── Signals                 (data emitted from runs, analytics, providers, humans)
        └── Learning Records    (structured, classified, timestamped interpretations of signals)
            └── Recommendations (actionable proposals derived from learning records)
                └── Approvals   (human or auto decision on each recommendation)
                    └── Applied Improvements  (confirmed changes with rollback path)
                        └── Monitoring        (post-application measurement)
                            └── Confirmation or Rollback
```

### What Each Layer Means

**Runs** are the raw event source. Every agent run, every episode, every retry, every gate pass or fail produces run records. Runs are the ground truth — they happened, they are immutable.

**Signals** are derived observations extracted from runs and external sources. A signal says: "this metric moved this way." A signal is not yet a conclusion. Signals have a source, a type, a value, a confidence, and a timestamp.

**Learning Records** are the intelligence layer's interpretation of signals. A learning record says: "based on signals A, B, and C, we have learned X about Y." Learning records are classified by scope (run-level, channel-level, factory-level), by domain (quality, cost, audience, provider), and by confidence level. Learning records require a minimum evidence threshold to be created.

**Recommendations** are actionable proposals derived from one or more learning records. A recommendation says: "given what we have learned, we propose change Z." Recommendations are typed, scoped, confidence-scored, evidence-linked, and impact-estimated.

**Approvals** are decisions. Each recommendation goes through an approval process — auto-apply (for low-risk, high-confidence changes) or human-approve (for high-risk or low-confidence changes). Approval records who decided, when, and on what evidence.

**Applied Improvements** are confirmed changes. An applied improvement has a rollback path defined before application. After application, it enters a monitoring window.

**Monitoring** is the post-application measurement phase. The system compares post-change metrics against pre-change baseline. If improvement is confirmed, the recommendation is marked `confirmed`. If metrics regress, rollback is triggered.

### Separation of Concerns

| Layer | Asks | Produces |
|---|---|---|
| Runs | What happened? | Event data |
| Signals | What moved? | Observations |
| Learning Records | What does it mean? | Interpreted knowledge |
| Recommendations | What should we do? | Proposals |
| Approvals | Should we do it? | Decisions |
| Applied Improvements | Did we do it safely? | Changes with rollback |
| Monitoring | Did it work? | Confirmation or rollback |

The architecture enforces this separation. No layer skips another.

---

## 2. Learning Inputs

### Platform Analytics Inputs

| Source | Signals Ingested |
|---|---|
| YouTube Analytics | Views, watch time, CTR, impressions, retention curve (per timestamp), subscribers gained/lost, revenue, traffic sources, top-performing videos, playlist performance |
| Meta Analytics | Reel views, saves, shares, reach, engagement rate, story exits, link clicks, audience demographics |
| TikTok Analytics | Views, watch completion rate, shares, comments, followers gained, traffic source (FYP vs following), sound performance |

Platform analytics are ingested on a defined cadence (not real-time). Minimum latency before use: 48 hours for YouTube (algorithm stabilization), 24 hours for Meta and TikTok.

### Internal Run Inputs

| Source | Signals Ingested |
|---|---|
| Agent run records | Success rate, retry rate, failure rate, cost per run, latency, schema validity rate, gate pass rate, output quality score, human override rate |
| Episode run records | Total cost per episode, total latency, pipeline failure points, retry chains, gate failures |
| Provider benchmarks | Quality score per model per capability, cost per output, latency per output, availability uptime |
| Model benchmarks | Quality comparison across models on standardized prompt suites |
| Cost records | Per-agent cost trends, per-episode cost, per-channel cost, budget burn rate |

### Archive Inputs

| Source | Signals Ingested |
|---|---|
| Archive reuse results | Which archived assets were reused, in which context, with what gate result, at what cost saving |
| Archive hit rate | How often archive lookup succeeds vs. misses per asset type |
| Archive quality drift | Whether reused assets pass current quality gates at the same rate as fresh assets |

### Human Inputs

| Source | Signals Ingested |
|---|---|
| Human overrides | Which agent outputs were corrected, what was changed, what type of correction |
| Approval decisions | Which recommendations were approved, rejected, or modified |
| Rejection reasons | Free-text or structured reason when a recommendation is rejected |
| Manual annotations | Human ratings applied to outputs or episodes |

### Signal Metadata (Required on Every Signal)

Every signal ingested must carry:

```yaml
signal:
  signal_id: "sig_<uuid>"
  source: "youtube_analytics"            # source system
  source_entity_id: "video_<id>"         # what entity the signal is about
  signal_type: "retention_curve"         # typed classification
  signal_scope: "episode"                # run | episode | channel | factory | provider | agent
  value: {...}                           # structured or scalar value
  measured_at: "2026-06-11T00:00:00Z"   # when the underlying event occurred
  ingested_at: "2026-06-13T09:00:00Z"   # when we received it
  confidence: 0.90                       # how reliable is the measurement (0.0–1.0)
  sample_size: 1240                      # number of data points behind this signal
  min_sample_threshold_met: true         # whether sample_size meets policy minimums
  expires_at: "2026-07-13T09:00:00Z"    # signals expire; stale signals cannot anchor recommendations
```

---

## 3. Learning Outputs

### Topic Recommendations

What to make next, and why.

```
Output: topic_recommendation
Content: suggested_topic, supporting_signals, confidence, channel_fit_score,
         estimated_ctr_range, estimated_watch_time_range, competitor_gap_analysis,
         recommended_publish_window
```

Examples:
- "Topic X is trending in your audience segment with CTR 2× your channel average"
- "Topic Y has low competition and matches your template strengths — recommended for next slot"
- "Topic Z performed poorly on this channel but strongly on comparable channels — cross-channel signal"

### Thumbnail Recommendations

What visual strategy is working.

```
Output: thumbnail_recommendation
Content: style_recommendation, color_palette_signal, text_density_signal,
         face_vs_no_face_performance, expression_type_signal, composition_signal,
         evidence_episodes, confidence
```

### Model Recommendations

Which model to assign to which agent.

```
Output: model_recommendation
Content: agent_id, current_model, recommended_model, benchmark_evidence,
         cost_delta_usd, quality_delta_score, latency_delta_ms,
         confidence, approval_policy_required
```

### Provider Recommendations

Which provider to route generation tasks to.

```
Output: provider_recommendation
Content: capability, current_provider, recommended_provider, benchmark_evidence,
         cost_delta_usd, quality_delta_score, latency_delta_ms,
         confidence, approval_policy_required
```

### Agent Improvement Recommendations

Which agents need attention and what kind.

```
Output: agent_improvement_recommendation
Content: agent_id, improvement_type (prompt_update | skill_upgrade | model_swap | replacement),
         trigger_metrics, evidence_window, confidence, estimated_impact
```

### Prompt Update Recommendations

When an agent's prompt is drifting or can be improved.

```
Output: prompt_update_recommendation
Content: agent_id, prompt_pack_id, drift_signals, suggested_focus_areas,
         evidence_episodes, confidence
         # Note: does not propose prompt text — that is authored by humans
```

### Budget Recommendations

Where to spend more and where to cut.

```
Output: budget_recommendation
Content: recommendation_type (reallocate | reduce | increase),
         from_agent_or_capability, to_agent_or_capability,
         cost_signal, quality_signal, estimated_monthly_delta_usd, confidence
```

### Schedule Recommendations

When to publish on each platform.

```
Output: schedule_recommendation
Content: channel_id, platform, recommended_day_of_week, recommended_hour_utc,
         evidence_episodes, audience_signal, confidence
```

### Platform Strategy Recommendations

Broader format, frequency, or platform-mix shifts.

```
Output: platform_strategy_recommendation
Content: recommendation_type, affected_channels, evidence, rationale,
         estimated_impact, confidence, human_review_required: true
         # Always human_review_required — never auto-applied
```

---

## 4. Learning Memory Types

Learning memory persists interpretations across time, at different scopes. Memory does not store raw signals — it stores structured, interpreted knowledge derived from signals.

### Short-Term Run Memory

**Scope:** Single episode or recent run cluster (last 7 days)  
**Purpose:** Within-pipeline context. What happened in the last few runs that should affect the current run?  
**Contents:** Recent failure modes, retry patterns, gate failure reasons, model fallback events  
**Retention:** 7 days, then promoted to channel memory if significant  
**Access:** Episode Director, Agent Director  
**Cross-channel:** No

### Channel Memory

**Scope:** Per channel, across all episodes  
**Purpose:** What has worked and failed for this specific channel's audience and template?  
**Contents:** Topic performance history, thumbnail strategy signals, CTR and retention trends, audience segment profile, best-performing formats, recurring failure patterns, override history  
**Retention:** Indefinite (with expiry on individual signals based on their own expiry policy)  
**Access:** Topic Intelligence Agent, Thumbnail Intelligence Agent, Audience Analyst, Factory Strategist  
**Cross-channel:** Read-only from other channels if cross-channel learning is enabled in channel config

### Template Memory

**Scope:** Per template (e.g., Hypogeum), across all channels using the template  
**Purpose:** What strengths and weaknesses are inherent to this template?  
**Contents:** Common failure modes, strong capabilities, model performance on template-specific prompts, gate pass rates by stage, prompt pack version history  
**Retention:** Indefinite  
**Access:** Template Director, Factory Strategist

### Factory-Wide Memory

**Scope:** All channels, all templates, all agents  
**Purpose:** What patterns hold across the entire factory?  
**Contents:** Cost trends, provider reliability history, model performance trends, cross-channel topic performance, global failure modes, governance decisions  
**Retention:** Indefinite  
**Access:** Factory Strategist, Agent Director, Provider & Model Director  
**Cross-channel:** Yes — this is the aggregated view; individual channel data is anonymized unless cross-channel sharing is enabled

### Provider/Model Memory

**Scope:** Per provider and model  
**Purpose:** What do we know about each provider's and model's behavior, cost, and quality over time?  
**Contents:** Benchmark history, cost history, availability events, quality trends, known failure modes, migration history  
**Retention:** Indefinite  
**Access:** Provider & Model Director, Cost Intelligence Agent, Provider Learning Agent

### Agent Memory

**Scope:** Per agent, per version  
**Purpose:** What does this specific agent's run history tell us about its behavior?  
**Contents:** Performance metric trends, common failure modes, prompt drift signals, model assignment history, override patterns, downstream failure attribution  
**Retention:** Indefinite (per agent version; archived versions retained in read-only form)  
**Access:** Agent Director, Agent Learning Agent, Run Analyst

### Audience Memory

**Scope:** Per channel (and factory-wide aggregated)  
**Purpose:** What do we know about who is watching, what they respond to, and when?  
**Contents:** Audience demographic signals, retention curve patterns, CTR response patterns, watch time patterns, comment sentiment signals (if available), subscriber behavior  
**Retention:** Rolling 90-day active window; older data archived  
**Access:** Audience Analyst, Topic Intelligence Agent, Thumbnail Intelligence Agent, Factory Strategist  
**Cross-channel:** Factory-wide audience memory aggregates without channel attribution

---

## 5. Intelligence Agents

Intelligence Agents are a specialized category within the Agent OS. They consume learning inputs, produce learning records and recommendations, and communicate exclusively through the recommendation lifecycle. They do not modify agents, models, or prompts directly.

### Run Analyst

**Capability:** `learning.analyze`  
**Purpose:** Processes run records to detect patterns in failures, retries, costs, and latency.  
**Inputs:** Run records, agent performance history, gate logs  
**Outputs:** Agent memory updates, agent improvement recommendations, cost intelligence signals  
**Cadence:** After every episode completion, and on-demand  
**Model:** Claude Sonnet tier — structured analysis, not reasoning-heavy  
**Notes:** This is the primary agent that feeds other intelligence agents with pre-processed run data

### Audience Analyst

**Capability:** `learning.audience`  
**Purpose:** Interprets platform analytics to build and update audience memory.  
**Inputs:** YouTube analytics, Meta analytics, TikTok analytics, audience memory (current state)  
**Outputs:** Audience memory updates, retention curve signals, CTR signals, watch time signals  
**Cadence:** Daily (after analytics batch ingestion)  
**Model:** Claude Sonnet tier  
**Notes:** Does not produce recommendations directly — feeds Topic Intelligence and Thumbnail Intelligence

### Topic Intelligence Agent

**Capability:** `learning.topic`  
**Purpose:** Identifies what topics to make next based on audience signals, performance history, and trend data.  
**Inputs:** Audience memory, channel memory, factory-wide memory (topic performance), platform analytics  
**Outputs:** Topic recommendations, topic performance learning records  
**Cadence:** Weekly, or triggered by schedule gap detection  
**Model:** Claude Sonnet or Opus tier (strategic synthesis required)  
**Notes:** Recommendations must cite evidence episodes and confidence scores. No auto-apply — always human approval.

### Thumbnail Intelligence Agent

**Capability:** `learning.thumbnail`  
**Purpose:** Identifies thumbnail strategies that drive CTR across channels and episodes.  
**Inputs:** Thumbnail performance signals, CTR signals, audience memory, channel memory  
**Outputs:** Thumbnail recommendations, thumbnail style learning records  
**Cadence:** After every 5 published episodes, or on-demand  
**Model:** Claude Sonnet tier  
**Notes:** Recommendations inform the Thumbnail Plan Agent's prompt pack. They do not update the prompt pack directly.

### Retention Analyst

**Capability:** `learning.retention`  
**Purpose:** Identifies drop-off points in retention curves and recommends script or pacing adjustments.  
**Inputs:** Retention curve signals per episode, script structure, segment timing from run records  
**Outputs:** Script structure recommendations, pacing recommendations, audience memory updates  
**Cadence:** After retention data for each episode stabilizes (48 hours post-publish)  
**Model:** Claude Sonnet tier  
**Notes:** Retention curves are noisy in the first 24 hours — minimum 48-hour stabilization required before ingestion.

### Cost Intelligence Agent

**Capability:** `learning.cost`  
**Purpose:** Monitors cost trends and identifies budget optimization opportunities.  
**Inputs:** Cost records per agent, per episode, per provider; budget targets; run records  
**Outputs:** Budget recommendations, provider recommendations (cost-driven), agent cost signals  
**Cadence:** Daily  
**Model:** Claude Haiku tier — structured analysis, low complexity  
**Notes:** Cost recommendations must include estimated monthly savings delta. Cannot recommend quality reduction to hit cost targets — must propose cost-neutral or cost-reducing changes only.

### Agent Learning Agent

**Capability:** `learning.agent`  
**Purpose:** Synthesizes Run Analyst outputs into agent-specific improvement recommendations.  
**Inputs:** Agent memory, run records (via Run Analyst), human override history  
**Outputs:** Agent improvement recommendations, prompt update recommendations  
**Cadence:** Weekly, or triggered by Agent Director anomaly alert  
**Model:** Claude Sonnet tier  
**Notes:** This agent reads Agent Director detection outputs but does not duplicate Agent Director logic. Agent Director detects; Agent Learning Agent synthesizes and recommends.

### Provider Learning Agent

**Capability:** `learning.provider`  
**Purpose:** Maintains provider/model memory and surfaces recommendations about provider swaps.  
**Inputs:** Provider benchmark records, model benchmark records, cost records, availability events, provider/model memory  
**Outputs:** Provider recommendations, model recommendations, provider/model memory updates  
**Cadence:** After every benchmark run, and weekly summary  
**Model:** Claude Sonnet tier  
**Notes:** Works closely with Provider & Model Director. Provider Learning Agent maintains the memory and synthesizes recommendations; PMD executes approved swaps.

### Factory Strategist

**Capability:** `learning.strategy`  
**Purpose:** The highest-level intelligence agent. Synthesizes all other intelligence agent outputs into factory-wide strategic recommendations.  
**Inputs:** All learning records, all recommendation histories, factory-wide memory, channel memory (aggregated), provider/model memory  
**Outputs:** Platform strategy recommendations, budget recommendations (factory-wide), channel mix recommendations  
**Cadence:** Monthly, or triggered by significant signal cluster  
**Model:** Claude Opus tier — reasoning-heavy synthesis across broad evidence  
**Notes:** All Factory Strategist outputs are `human_review_required: true`. No auto-apply ever. Strategist recommendations are advisory — they inform human decisions, not trigger them.

---

## 6. Recommendation Lifecycle

### States

| State | Meaning |
|---|---|
| `draft` | Recommendation generated by an intelligence agent but not yet evaluated |
| `evaluated` | Confidence score assigned; evidence reviewed; impact estimated |
| `proposed` | Surfaced to dashboard and/or approval queue |
| `approved` | Human (or auto-policy) has approved application |
| `applied` | Change has been executed; monitoring window begins |
| `monitored` | Post-application metrics are being tracked |
| `confirmed` | Post-application metrics meet or exceed expected improvement; recommendation closed |
| `rolled_back` | Post-application metrics regressed; change reversed; recommendation closed as failed |

### State Machine

```
[draft]
  → evaluate → [evaluated]        (intelligence agent scores and validates)

[evaluated]
  → propose  → [proposed]         (surfaced to approval queue or dashboard)
  → discard  → terminal           (confidence too low; evidence insufficient)

[proposed]
  → approve  → [approved]         (human approves, or auto-policy triggers)
  → reject   → terminal           (human rejects; reason recorded)
  → expire   → terminal           (no decision within policy window; auto-expired)

[approved]
  → apply    → [applied]          (change executed; rollback path confirmed)

[applied]
  → monitor  → [monitored]        (monitoring window starts)

[monitored]
  → confirm  → [confirmed]        (metrics confirm improvement)
  → rollback → [rolled_back]      (metrics regress; change reversed)
```

### Recommendation Record

```yaml
recommendation:
  recommendation_id: "rec_<uuid>"
  recommendation_type: "model_recommendation"
  source_agent: "provider-learning-agent"
  source_learning_records: ["lr_<uuid>", "lr_<uuid>"]
  
  # Target
  target_type: "agent"             # agent | provider | channel | factory | topic | thumbnail | budget | schedule | platform
  target_id: "agt_<uuid>"
  
  # Confidence and evidence
  confidence: 0.87                 # 0.0–1.0
  evidence_summary: "..."          # human-readable rationale
  evidence_signal_ids: ["sig_<uuid>", ...]
  evidence_episode_ids: ["ep_<uuid>", ...]
  sample_size: 43                  # data points behind recommendation
  
  # Impact estimate
  estimated_impact:
    quality_delta: +0.04
    cost_delta_usd_per_month: -12.50
    latency_delta_ms: -800
    confidence_in_estimate: 0.72
  
  # Approval
  approval_policy: "director_recommend_human_approve"
  auto_apply_eligible: false
  
  # Lifecycle
  status: "proposed"
  created_at: "2026-06-13T10:00:00Z"
  evaluated_at: "2026-06-13T10:01:00Z"
  proposed_at: "2026-06-13T10:01:30Z"
  expires_at: "2026-07-13T10:01:30Z"
  approved_at: null
  approved_by: null
  applied_at: null
  monitoring_ends_at: null
  confirmed_at: null
  rolled_back_at: null
  rejection_reason: null
  
  # Rollback
  rollback_plan: "restore_agent_model_to_claude-sonnet-4-6"
  rollback_requires_approval: false
```

### Recommendation Expiry

Every recommendation has an expiry date. If no approval decision is made before expiry, the recommendation is automatically moved to terminal state with reason `expired`. An expired recommendation requires a fresh evaluation before it can be re-proposed. The default expiry window is 30 days.

---

## 7. Approval Rules

### Auto-Apply Eligible

These recommendation types may be auto-applied if their confidence score meets the threshold and their policy permits. Auto-apply is an opt-in configuration per recommendation type, not the default.

| Recommendation Type | Auto-Apply Threshold | Conditions |
|---|---|---|
| Archive reuse | Any confidence | Zero-cost, zero-risk change |
| Schedule adjustment | Confidence ≥ 0.90, sample ≥ 20 episodes | No cost impact |
| Cost routing (equal quality) | Confidence ≥ 0.92, benchmark window current | Quality must be statistically equivalent |
| Prompt update (minor, patch) | Confidence ≥ 0.95, human reviewed prompt text | Prompt text authored by human; auto-apply only triggers the version bump |

### Human Approval Required

These recommendation types always require a human decision before application.

| Recommendation Type | Reason |
|---|---|
| Model swap | Affects agent behavior at the capability level |
| Provider swap | External dependency change; cost and quality impact |
| Agent replacement | Workforce decision; potentially irreversible |
| Topic recommendation | Creative direction; brand alignment required |
| Thumbnail strategy change | Brand-adjacent; affects audience perception |
| Budget reallocation | Financial governance |
| Prompt update (major) | Significant behavior change; requires human authorship and review |
| Platform strategy | High-impact, broad-scope change |

### Never Auto-Applied

These recommendation types require human approval regardless of confidence score or policy configuration.

| Recommendation Type | Reason |
|---|---|
| Factory Strategist outputs | Too broad; could affect multiple channels simultaneously |
| Platform strategy recommendations | Irreversible audience positioning decisions |
| Agent retirement (archive) | Workforce governance |
| Cross-channel strategy changes | Multi-stakeholder impact |
| Any recommendation affecting a client channel | Client governance required |
| Any recommendation with negative quality delta | Quality regression is never auto-accepted |

### Approval Record

Every approval (or rejection) is recorded with:
- Decision: `approved` | `rejected` | `modified`
- Actor: named human or `auto_policy` (with policy ID)
- Timestamp
- Modification note (if decision was `modified` — human changed parameters before approving)
- Rejection reason (if `rejected`)

---

## 8. Learning Safety Rules

### Data Sufficiency Rules

| Rule | Policy |
|---|---|
| Minimum signal sample size | Configurable per recommendation type (default: 10 runs; audience signals: 500 impressions) |
| Minimum stabilization window | Platform analytics: 48 hours post-publish before ingestion |
| No learning from single-run data | One run is never sufficient evidence for any recommendation |
| Benchmark expiry | Model/provider benchmarks expire at 30 days; expired benchmarks cannot anchor recommendations |
| Retention curve minimum | Retention curve requires minimum 200 views before use |

### Confidence Threshold Rules

| Rule | Policy |
|---|---|
| Minimum confidence to propose | 0.70 — below this, recommendation is discarded in evaluation |
| Minimum confidence to auto-apply | 0.90 for schedule; 0.92 for cost routing; 0.95 for prompt patch |
| Minimum confidence for strategy recommendations | 0.80 — even human-approved; below this, recommendation is flagged as `low_confidence_advisory` |
| Conflicting recommendation resolution | If two recommendations conflict and confidence gap < 0.10, neither is auto-applied; human reviews both |

### Model and Provider Swap Safety Rules

| Rule | Policy |
|---|---|
| No model swap from one weak signal | Minimum 3 independent signal types required |
| No provider swap from benchmark alone | Benchmark + production run evidence required |
| Post-migration monitoring window | Minimum 10 production runs before migration confirmed |
| No swap during active run | Registry enforced (from Agent OS safety rules) |
| Rollback required for every swap | Rollback plan must be confirmed before apply step executes |

### Strategy Safety Rules

| Rule | Policy |
|---|---|
| No strategy change without confidence threshold | Platform strategy requires ≥ 0.80 confidence |
| No strategy change from single analytics source | Multi-platform signal required for platform strategy |
| No frequency change from short window | Minimum 30-day analytics window for publish cadence changes |
| All platform strategy recommendations are advisory | `human_review_required: true` always set |

### Cross-Channel and Privacy Rules

| Rule | Policy |
|---|---|
| No cross-channel contamination unless allowed | Channel config must explicitly enable `cross_channel_learning: true` |
| Client data isolation | Client channel data never leaks into factory-wide memory without explicit consent |
| Audience memory anonymization | Factory-wide audience memory aggregates without channel attribution |
| No recommendation targeting a client channel from internal data without disclosure | Policy violation; blocked at recommendation generation |

### Auto-Apply Safety Rules

| Rule | Policy |
|---|---|
| No auto-apply for high-risk changes | Risk classification checked before auto-apply trigger |
| No auto-apply for quality-reducing changes | Quality delta must be ≥ 0 for auto-apply |
| No auto-apply for changes to `locked` agents | Registry enforces |
| Auto-apply log required | Every auto-applied change is logged with full recommendation record and policy ID |
| Weekly auto-apply digest | All auto-applied changes surfaced in dashboard weekly digest for human review |

### Rollback Rules

| Rule | Policy |
|---|---|
| Rollback path required before apply | System refuses to execute apply without a confirmed rollback plan |
| Rollback monitoring window | After rollback, monitoring continues for minimum 5 runs to confirm stability |
| Rollback requires no new approval | If rollback policy is `requires_approval: false` (default for most changes) |
| Re-proposal after rollback | A rolled-back recommendation requires new evidence before re-proposal |
| Cascading rollback | If a rolled-back change affected downstream agents, rollback plan must include downstream steps |

---

## 9. Dashboard Integration

### Learning Feed Panel

Real-time stream of new learning records and signals.

- Signal source, type, entity, timestamp, confidence
- Filter by: scope (run/episode/channel/factory), domain (quality/cost/audience/provider), agent
- Click-through to full signal detail and associated learning records

### Recommendations Panel

All active recommendations across the factory.

- Recommendation type, target, confidence, status
- Evidence summary (expandable)
- Expected impact: quality delta, cost delta, latency delta
- Confidence in impact estimate
- Approval status: pending | approved | rejected | expired
- Filter by: type, status, target, auto-apply eligible, confidence range
- Sort by: confidence, estimated impact, expiry date

### Recommendation Detail View

- Full recommendation record
- Evidence signal list (with confidence and sample size per signal)
- Evidence episode list (click-through to episode detail)
- Impact estimate breakdown
- Approval history
- Rollback plan summary
- Approve / Reject / Modify controls (for human-approval recommendations)

### Confidence Score Panel

- Distribution of recommendation confidence scores (histogram)
- Average confidence by recommendation type
- Confidence trend over time
- Low-confidence recommendations flagged for review

### Applied Changes Panel

- All applied recommendations (last 30 days)
- Current monitoring status per applied change
- Post-application metric comparison (before vs. after)
- Confirmation or rollback status

### Rollback Status Panel

- Recommendations currently in monitoring window
- Metric trends during monitoring (real-time)
- Days remaining in monitoring window
- Rollback trigger thresholds (visual indicator of proximity)
- Manual rollback control (human can trigger rollback at any time during monitoring)

### Learning Health Panel

- Intelligence agent status (active, inactive, last run)
- Signal ingestion health (last ingestion timestamp per source)
- Memory health (last updated per memory type, anomalies)
- Recommendation pipeline health (draft → confirmed flow rate)
- Stale recommendation count (pending beyond 14 days)
- Expired recommendation count (last 30 days)
- Rollback rate (applied recommendations that were rolled back, as a percentage)

---

## 10. API Contracts

These are interface contracts only. No implementation details.

### `record_learning_signal()`

```
Purpose: Ingest a structured signal from any source into the learning pipeline.

Input:
  source: string                       # source system identifier
  source_entity_id: string             # entity the signal is about
  signal_type: string                  # typed classification
  signal_scope: enum                   # run | episode | channel | factory | provider | agent
  value: object                        # structured or scalar value
  measured_at: datetime
  confidence: float [0.0–1.0]
  sample_size: int
  metadata: object (optional)

Output:
  signal_id: string
  min_sample_threshold_met: bool
  ingested: bool
  rejection_reason: string | null

Guarantees:
  - Idempotent on (source, source_entity_id, signal_type, measured_at)
  - Rejects signals below minimum sample size threshold for their type
  - Signals are immutable after ingestion
```

### `generate_recommendations()`

```
Purpose: Trigger an intelligence agent to process current learning records and generate recommendations.

Input:
  agent_capability: string             # which intelligence agent to invoke
  scope: enum                          # run | episode | channel | factory | provider | agent
  scope_id: string | null              # specific entity; null = all in scope
  force: bool (default: false)         # bypass cadence check

Output:
  recommendations_generated: int
  recommendation_ids: [string]
  discarded_count: int                 # recommendations that failed confidence threshold
  execution_time_ms: int

Guarantees:
  - Does not modify any agent, model, prompt, or schedule
  - Does not apply any changes
  - Idempotent if called within same cadence window (returns cached result unless force=true)
```

### `evaluate_recommendation()`

```
Purpose: Score and validate a draft recommendation before it can be proposed.

Input:
  recommendation_id: string

Output:
  recommendation_id: string
  status: enum (evaluated | discarded)
  confidence: float
  estimated_impact: object
  evidence_validated: bool
  discard_reason: string | null

Guarantees:
  - Evaluation is deterministic for the same inputs
  - A discarded recommendation cannot be re-evaluated without new evidence
  - Does not modify target entity
```

### `approve_recommendation()`

```
Purpose: Record a human approval decision on a proposed recommendation.

Input:
  recommendation_id: string
  decision: enum (approved | rejected | modified)
  actor: string                        # named human or "auto_policy:<policy_id>"
  modification: object | null          # if decision is modified, what changed
  rejection_reason: string | null

Output:
  recommendation_id: string
  status: enum (approved | rejected)
  next_step: string                    # "ready_to_apply" | "closed"

Guarantees:
  - Rejected recommendations cannot be re-approved without re-evaluation
  - Approval record is immutable after creation
  - `auto_policy` actor requires policy_id to be valid and registered
```

### `apply_recommendation()`

```
Purpose: Execute an approved recommendation. Begins monitoring window.

Input:
  recommendation_id: string
  confirm_rollback_plan: bool          # must be true; rejected otherwise

Output:
  recommendation_id: string
  status: enum (applied | failed)
  applied_at: datetime | null
  monitoring_ends_at: datetime | null
  failure_reason: string | null

Guarantees:
  - Refuses to apply if rollback plan is not confirmed
  - Refuses to apply if target entity has an active run
  - Creates an immutable apply record before making any changes
  - Monitoring window begins immediately after apply record is created
```

### `monitor_recommendation()`

```
Purpose: Read current post-application metrics for a recommendation in monitoring window.

Input:
  recommendation_id: string

Output:
  recommendation_id: string
  monitoring_status: enum (in_progress | confirmed | rollback_triggered)
  runs_since_apply: int
  runs_required_to_confirm: int
  metric_comparison:
    pre_apply: object
    post_apply: object
    delta: object
  rollback_trigger_proximity: float    # 0.0 = far from trigger; 1.0 = at threshold

Guarantees:
  - Read-only; does not modify recommendation state
  - Returns current snapshot; not cached
```

### `rollback_recommendation()`

```
Purpose: Reverse an applied recommendation and record rollback.

Input:
  recommendation_id: string
  trigger: enum (auto_threshold | human_manual)
  reason: string (required if trigger is human_manual)

Output:
  recommendation_id: string
  status: enum (rolled_back | rollback_failed)
  rolled_back_at: datetime | null
  rollback_failure_reason: string | null

Guarantees:
  - Executes the rollback plan defined at apply time
  - Creates an immutable rollback record
  - Post-rollback monitoring window begins for minimum 5 runs
  - Rolled-back recommendation is permanently closed; re-proposal requires new evidence
```

### `get_learning_memory()`

```
Purpose: Read the current state of a learning memory store.

Input:
  memory_type: enum (short_term_run | channel | template | factory | provider_model | agent | audience)
  scope_id: string | null              # specific entity; null = all in scope
  as_of: datetime | null              # point-in-time query; null = current
  include_expired_signals: bool (default: false)

Output:
  memory_type: string
  scope_id: string | null
  last_updated_at: datetime
  record_count: int
  records: [object]                    # structured learning records
  expired_signal_count: int            # signals that have expired and are excluded

Guarantees:
  - Read-only
  - Expired signals are excluded from default output
  - Point-in-time queries (`as_of`) use immutable signal timestamps
  - Does not trigger new learning or recommendations
```

---

## 11. Risks and Mitigations

### False Learning

**Risk:** The system learns a wrong lesson from coincidental correlation. A topic performs well because of an external event (viral news moment), not because of the template or format. The system recommends replicating something that was luck, not skill.

**Mitigations:**
- Multi-signal requirement: no recommendation from single analytics source.
- Minimum sample size enforced; single outlier episodes cannot drive recommendations.
- Confidence scoring must reflect sample size — small samples yield low confidence even if signal is strong.
- Human approval required for all topic and strategy recommendations, giving humans a chance to recognize false learning.
- Retention Analyst and Audience Analyst cross-validate signals; a signal that does not appear in both is flagged as `single_source`.

### Overfitting

**Risk:** The intelligence layer optimizes so tightly for past performance patterns that it stops producing creative or novel content. Every recommendation converges on the same formats, topics, and thumbnails. The channel becomes formulaic and audience growth stalls.

**Mitigations:**
- Topic Intelligence Agent includes `novelty_score` in each recommendation — how different is this from recent recommendations?
- Factory Strategist is required to flag if topic recommendations have been converging for 4+ consecutive weeks.
- Thumbnail Intelligence Agent must report on style diversity; if style variance drops below threshold, an alert is emitted.
- Human editorial override is always available and is never penalized in the learning model — overrides are signals, not errors.
- `exploration_budget`: a configured percentage of episodes per period that are explicitly not recommendation-driven.

### Bad Analytics

**Risk:** Platform analytics are incorrect, delayed, sampled, or attributing performance to the wrong factors. YouTube analytics in particular are known to fluctuate significantly in the first 48 hours.

**Mitigations:**
- Minimum stabilization window: 48 hours for YouTube, 24 hours for Meta/TikTok.
- Anomaly detection on ingestion: if a signal value is > 3× historical average for that entity type, it is flagged for review before being used.
- Analytics confidence score degrades for entities with fewer than minimum sample sizes.
- No recommendation is anchored to a single analytics data point.
- Conflicting signals (e.g., high CTR but low watch time) are surfaced as conflicting — not synthesized into a false consensus.

### Platform Bias

**Risk:** One platform's analytics dominate recommendations because it is better instrumented or has more data. YouTube (which has the richest analytics) may bias all recommendations, even for content that primarily targets TikTok.

**Mitigations:**
- Per-platform signal weighting: recommendations scoped to a platform use only that platform's analytics as primary signals.
- Cross-platform recommendations require signals from all relevant platforms.
- Audience Analyst tracks per-platform signal quality and flags imbalances.
- Platform strategy recommendations must be reviewed against per-platform audience memory, not factory-wide aggregated memory.

### Data Leakage

**Risk:** Learning from one client's channel contaminates the factory-wide memory, exposing proprietary performance data or audience insights to other clients.

**Mitigations:**
- Cross-channel learning is opt-in at channel config level; default is off.
- Factory-wide memory aggregates anonymize channel attribution.
- Client channel data is tagged with `client_owned: true`; these signals are excluded from factory-wide memory unless consent is configured.
- `get_learning_memory()` enforces scope isolation — cannot read another channel's memory without explicit authorization.
- Data leakage is treated as a safety rule violation, not a configuration error.

### Over-Automation

**Risk:** Too many recommendations are auto-applied, compounding errors faster than humans can review them. A bad recommendation about model assignment is auto-applied, which changes output quality, which generates more bad signals, which generates more bad recommendations.

**Mitigations:**
- Auto-apply is opt-in, not default.
- Auto-apply is limited to a small set of low-risk, high-confidence recommendation types.
- Recommendation cascade detection: if more than N auto-applied recommendations reference the same parent signal within a 7-day window, further auto-apply from that signal is paused pending human review.
- Weekly auto-apply digest is non-optional — humans always see what was auto-applied.
- Quality delta ≥ 0 is an absolute requirement for auto-apply — quality-neutral or quality-improving changes only.

### Conflicting Recommendations

**Risk:** Two intelligence agents produce contradictory recommendations. Cost Intelligence recommends switching to a cheaper model; Agent Learning recommends the current model is optimal. Both have high confidence. The system paralyzes or, worse, applies both.

**Mitigations:**
- Conflict detection is built into the evaluation step: if a new recommendation contradicts an active pending recommendation, both are flagged as `conflicting`.
- Conflicting recommendations cannot be auto-applied.
- Dashboard surfaces conflicting recommendations in a dedicated conflict review view.
- Conflict resolution policy: human reviews both and decides; or higher-confidence recommendation wins if confidence gap > 0.15.
- Intelligence agents do not see each other's draft recommendations — they reason independently to prevent groupthink.

### Stale Recommendations

**Risk:** A recommendation is proposed but not acted on for weeks. By the time it is approved and applied, the underlying signals have changed. The applied recommendation is based on outdated evidence.

**Mitigations:**
- Recommendation expiry window: 30 days by default; configurable per recommendation type.
- At apply time, the system re-validates that source signals have not expired.
- If any source signal has expired between proposal and apply, the apply is blocked and the recommendation is returned to `evaluated` state for re-evaluation.
- Dashboard surfaces recommendations approaching expiry with a visual indicator.
- Long-pending recommendations trigger a dashboard alert at 14 days (half the expiry window).

### Cross-Contamination Summary

All of these risks share a common mitigation principle: **scope isolation and evidence provenance**. Every signal, learning record, and recommendation carries its scope, source, sample size, and expiry. The system refuses to reason beyond its evidence. When it is uncertain, it says so — and routes to human review rather than auto-applying.

---

## Summary: Design Principles

1. **Data is not learning.** Signals become learning records only after minimum sample, confidence scoring, and scope validation.
2. **Recommendations are proposals, not orders.** The intelligence layer advises; humans and the Agent OS execute.
3. **Every applied change has a rollback.** Applied without a rollback plan = system refusal.
4. **Scope isolation is the first privacy rule.** Cross-channel learning requires explicit opt-in.
5. **Quality cannot regress via auto-apply.** Any recommendation with a negative quality delta requires human approval.
6. **Expired evidence cannot drive decisions.** Stale signals, stale benchmarks, and stale recommendations are blocked at apply time.
7. **The system is honest about uncertainty.** Low confidence → discard or advisory, never false precision.
8. **Factory Strategist is always advisory.** The highest-level agent produces no auto-applied changes.

---

*Document: docs/learning-intelligence-layer.md*  
*Phase: B6.5 — Design Only*  
*No implementation. No database changes. No code changes.*
