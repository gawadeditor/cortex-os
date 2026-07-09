# Learning Module Design

**Phase:** B5.7  
**Depends on:** B5.275 (OIS), B5.35 (Notification System), B5.4 (Approval System), B5.5 (Review System), B5.6 (Channel Profile), B6.5 (Learning Intelligence Layer), B4.9 (Engineering Director)  
**Feeds into:** Approval System, OIS, Channel Profile, Provider/Model Director, Engineering Director  
**Related design:** docs/learning-intelligence-layer.md (B6.5) — defines the intelligence agents that produce learning signals; B5.7 defines the module governance layer those agents operate within.

---

## 1. Purpose and Position

The Learning Module is the self-improvement layer of Factory OS. It observes outcomes, detects patterns, generates recommendations, and sends approved recommendations into the governance flow.

The Learning Module does not execute changes. It proposes. The separation is absolute.

```
Analytics Layer         →  What happened?
Learning Module         →  What might improve?
OIS                     →  How urgent? Who needs to know?
Approval System         →  May it proceed?
Engineering / Downstream →  Execute the approved change.
```

This separation is not a design preference — it is a safety constraint. A learning system that can write to its own inputs creates feedback loops. A learning system that can execute changes without approval creates ungoverned mutations. B5.7 enforces the separation at the architecture level, not through policy alone.

### 1.1 Relationship to B6.5

B6.5 (Learning Intelligence Layer) defines the intelligence agents that produce learning signals and recommendations: Run Analyst, Audience Analyst, Topic Intelligence Agent, Thumbnail Intelligence Agent, Retention Analyst, Cost Intelligence Agent, Agent Learning Agent, Provider Learning Agent, Factory Strategist.

B5.7 defines the governance module those agents operate within: the signal submission contract, the recommendation lifecycle, the confidence model, the suppression rules, the write-back restrictions, the OIS routing integration, and the dashboard surface.

B6.5 is the intelligence implementation. B5.7 is the governance container.

---

## 2. Architecture

### 2.1 Inputs

| Input source | What the Learning Module receives |
|---|---|
| Analytics Layer | Performance signals: CTR, retention, watch time, subscriber growth, revenue, engagement by platform and episode |
| Review System | Rejection reasons, gate failure patterns, override frequency, review age trends, approval/rejection ratios |
| Approval System | Recommendation decision outcomes: approved, rejected, deferred, with reasons |
| OIS | Routing outcomes for submitted signals (passive_insight / recommendation / approval_request / suppressed) |
| Publishing Pipeline | Publishing outcomes: platform confirmation, delivery failures, scheduling deviations |
| Acceptance Audit | Gate verdict history: gate_type, pass/fail ratio, flag patterns, failure stage distribution |
| Channel Profile | Current audience demographics, content pillars, platform strategy, learning goals, auto_apply_threshold |
| Provider/Model Director | Model benchmark results, provider quality events, cost-per-output history, availability signals |
| Agent Director | Agent health events, agent performance scores, override rates, retry patterns |
| Cost Ledger | Per-episode cost, per-agent cost, per-capability cost, budget utilization rate |

### 2.2 Outputs

| Output | Destination |
|---|---|
| `learning_signal` | OIS (for routing to appropriate dashboard surfaces) |
| `recommendation` | OIS (for routing to Learning page, Home cards, Approval System) |
| `confidence_score` | Attached to every recommendation; readable on Learning page |
| `recommendation_history` | Stored in Learning memory; readable via `get_learning_history()` |
| `rejected_recommendation_memory` | Stored in Learning memory; drives suppression rules |
| `learning_report` | Generated on cadence; routed to OIS as passive_insight; displayed on Learning page |
| `channel_profile_update_proposal` | Routed via OIS → Approval System → Channel Profile Manager |
| `provider_model_signal` | Sent to Provider/Model Director for their benchmarking and swap proposal process |

### 2.3 The non-output constraint

The Learning Module never outputs:
- Direct writes to Channel Profile fields
- Direct model or provider swap commands
- Direct prompt modifications
- Direct agent configuration changes
- Direct publishing rule changes

These outputs are architecturally prohibited. The Learning Module's only write authority is to its own memory (learning records, suppression rules, recommendation history). Everything else is read-only from the Learning Module's perspective. Changes to other systems flow through the governance chain.

---

## 3. Learning Domains

The Learning Module observes thirteen domains. Each domain has a designated set of signal sources, a minimum evidence threshold, and one or more recommendation types it can generate.

| Domain | Signal sources | Min evidence threshold | Recommendation types generated |
|---|---|---|---|
| Content performance | Analytics, Publishing Pipeline | 5 episodes with stable analytics | content_recommendation |
| Title performance | Analytics (CTR, impressions) | 10 episodes, 5,000 impressions per batch | title_recommendation |
| Thumbnail performance | Analytics (CTR, impression stats) | 10 episodes, 5,000 impressions per batch | thumbnail_recommendation |
| Retention performance | Analytics (retention curves) | 5 episodes, 200 views each (48h stabilization) | content_recommendation, workflow_recommendation |
| Publishing timing | Analytics, Publishing Pipeline | 20 episodes across 3+ publish windows | publishing_recommendation |
| Platform performance | Analytics per platform | 10 episodes per platform with analytics | publishing_recommendation, channel_profile_recommendation |
| Audience behavior | Analytics demographics, retention, comments (if available) | 500 audience sessions | channel_profile_recommendation |
| Gate failure patterns | Acceptance Audit, Review System | 5 gate failures of same type in 30-day window | prompt_recommendation, workflow_recommendation |
| Human rejection patterns | Review System (rejection reasons) | 3 rejections with same reason_category | prompt_recommendation, workflow_recommendation |
| Model/provider performance | Provider/Model Director, Cost Ledger, Acceptance Audit | 10 production runs per model, current benchmark | model_recommendation (via PMD), provider_recommendation (via PMD) |
| Prompt performance | Acceptance Audit (gate pass rates), Review System (override rates), Agent Director (output quality scores) | 10 runs per agent per prompt version | prompt_recommendation |
| Cost efficiency | Cost Ledger, Provider/Model Director | 20 episodes of cost data | budget_recommendation, provider_recommendation (via PMD) |
| Workflow efficiency | Run OS (stage latency, retry rates), Agent Director | 15 runs with stage timing data | workflow_recommendation |

**Evidence thresholds are minimums, not targets.** Meeting the minimum threshold allows a recommendation to be generated with low confidence. Higher evidence counts produce higher confidence scores.

---

## 4. Recommendation Types

Ten recommendation types are defined. Each type has a scope, a target, a risk band, and an approval path.

### 4.1 content_recommendation

**What it proposes:** Which topics to produce next, or which content formats are performing. Does not prescribe the specific episode — that remains a creative and editorial decision.

**Target:** Channel, specific content pillar.

**Risk band:** LOW to MEDIUM. Content selection is always human-reviewed — the operator chooses whether to act.

**Approval path:** Human approval always required. Never auto-applied. Operator+ authority.

**Example:** "Topics in the 'Underground Roman Engineering' cluster are driving 2× your channel average CTR based on 8 episodes. Recommend increasing ARCHAEOLOGY pillar weight from 45% to 55% for next quarter."

### 4.2 title_recommendation

**What it proposes:** Patterns in title phrasing, structure, or length that correlate with CTR performance. Does not write titles — it identifies patterns for the operator and Script Engine to use.

**Target:** Channel, specific pillar.

**Risk band:** LOW. Title patterns are informational; the operator decides how to use them.

**Approval path:** Passive insight if confidence < 0.80. Recommendation on Learning page if confidence ≥ 0.80. Never requires Approval System — operator reads and decides how to update guidance.

**Exception:** If the recommendation involves updating a Channel Profile field (e.g., `content_pillars[*].description` to include title pattern guidance), it requires the channel_profile_recommendation path and Approval System routing.

### 4.3 thumbnail_recommendation

**What it proposes:** Thumbnail style patterns correlated with CTR: color use, text density, subject positioning, face vs. no-face, emotional expression type.

**Target:** Channel, Thumbnail Director prompt pack.

**Risk band:** MEDIUM. Thumbnail strategy is brand-adjacent; changes affect audience perception.

**Approval path:** Human approval required (Operator+). Cannot auto-apply. Proposal is evidence-linked so the operator can verify the CTR correlation before acting.

### 4.4 publishing_recommendation

**What it proposes:** Adjustments to publish day, publish time, or publish cadence per platform.

**Target:** Channel platform_strategy.platform_configs per platform.

**Risk band:** LOW (time adjustment within same day) to MEDIUM (cadence change).

**Approval path:** Time adjustments with confidence ≥ 0.90 and sample ≥ 20 episodes may be auto-applied if channel's `auto_apply_threshold` permits. Cadence changes always require Operator+ approval. Platform strategy shifts (changing primary platform or disabling a platform) are always CRITICAL risk and require Admin+ approval.

### 4.5 prompt_recommendation

**What it proposes:** Areas where an agent's prompt guidance may be drifting or could be improved, based on gate failure patterns, override rates, or output quality scores. Does NOT write prompt text — it identifies the area of concern and the evidence.

**Target:** Specific agent, specific prompt pack component.

**Risk band:** MEDIUM (minor adjustment) to HIGH (major behavior change).

**Approval path:** Proposal is submitted to the operator as a recommendation. If approved, the operator (or a prompt engineer) authors the new prompt text. The Learning Module does not generate prompt text. After the operator updates the prompt and creates a new prompt pack version, the implementation is recorded as the approved change.

**Important constraint:** The Learning Module can identify THAT a prompt needs attention and WHERE, but the WHAT (the new prompt text) is always human-authored. This prevents the learning system from modifying its own input — the prompts are agent instructions, and a learning system that can rewrite agent instructions can rewrite the rules by which it is evaluated.

### 4.6 model_recommendation

**What it proposes:** A signal to the Provider/Model Director indicating that model performance evidence warrants a review. The Learning Module does NOT directly recommend a model swap — it sends a signal that triggers the PMD's own evaluation process.

**Target:** Provider/Model Director (not a direct channel or agent target).

**Risk band:** HIGH. Model changes affect all runs for the affected channel until changed back.

**Approval path:** The signal is sent to PMD. PMD runs its benchmarking process and generates the swap proposal if warranted. The swap proposal is what goes through the Approval System (model_swap type). The Learning Module's signal is not an approval item itself — it is a trigger to PMD.

### 4.7 provider_recommendation

**What it proposes:** Same pattern as model_recommendation. The Learning Module sends a signal to PMD indicating provider performance evidence warrants review. PMD evaluates and generates the swap proposal.

**Target:** Provider/Model Director.

**Risk band:** HIGH. Provider changes affect cost, quality, and availability for all runs using that provider.

**Approval path:** Signal to PMD → PMD generates provider_swap proposal → Approval System (model_swap type, HIGH risk).

### 4.8 channel_profile_recommendation

**What it proposes:** An update to one or more Channel Profile fields within the write-permitted set (§7).

**Target:** Specific Channel Profile fields.

**Risk band:** HIGH. Channel Profile changes affect all future runs for that channel.

**Approval path:** Routed via OIS → Approval System (Learning Recommendation type). The write-permitted fields require Operator+ approval. Fields outside the write-permitted set cannot be proposed at all — the Learning Module suppresses any internally-generated signals that would require touching forbidden fields.

### 4.9 workflow_recommendation

**What it proposes:** Adjustments to production workflow: stage ordering suggestions, retry policy adjustments, agent sequencing observations. Does not modify workflow config directly.

**Target:** Agent Director, Run OS workflow config.

**Risk band:** MEDIUM to HIGH. Workflow changes affect all future runs for the channel.

**Approval path:** MEDIUM risk requires Operator+ approval. HIGH risk requires Admin+. Implementation (if approved) is executed by Engineering Director.

### 4.10 budget_recommendation

**What it proposes:** Reallocation of budget between agents, capabilities, or platforms. Identifies where spend is high relative to quality output and where low spend may be limiting quality.

**Target:** Channel budget configuration, Cost Ledger policy.

**Risk band:** LOW (reallocation between equivalent-quality options) to MEDIUM (budget reduction for a non-critical agent).

**Approval path:** LOW risk with confidence ≥ 0.92 may be auto-applied if `auto_apply_threshold = "medium_and_above"`. MEDIUM risk always requires Operator+ approval. No budget_recommendation may propose reducing total budget — only reallocation or optimization. Total budget decisions are operator-only.

---

## 5. Recommendation Lifecycle

### 5.1 States

| State | Meaning |
|---|---|
| `generated` | Recommendation created by an intelligence agent (B6.5). Not yet scored or submitted. |
| `suppressed` | Recommendation generation was triggered but the signal matches an active suppression rule. Not surfaced. |
| `pending_approval` | Confidence threshold met; routed via OIS to Approval System or Learning page. Awaiting operator decision. |
| `approved` | Operator (or auto-policy) approved. Implementation may proceed. |
| `rejected` | Operator rejected. Reason recorded. Suppression check triggered. |
| `expired` | No decision made within the expiry window (default 30 days). Automatically closed. |
| `implemented` | Approved change has been executed by the downstream system (Engineering Director, PMD, Channel Profile Manager). Monitoring window begins. |
| `evaluated` | Monitoring window complete. Before/after performance compared. Outcome recorded: successful / neutral / failed / inconclusive. |
| `rolled_back` | Post-implementation metrics regressed. Change reversed by downstream system. Outcome = failed. |

### 5.2 State machine

```
generated
  ├─→ suppressed     (active suppression rule matches — terminal for this instance)
  └─→ [scored]
        ├─→ [confidence < 0.70] → discarded (below threshold — never surfaced)
        └─→ [confidence ≥ 0.70] → pending_approval

pending_approval
  ├─→ approved       (Approval System decision or auto-policy)
  ├─→ rejected       (Approval System decision — reason captured)
  └─→ expired        (no decision within expiry window)

approved
  └─→ implemented    (downstream system signals execution complete)

implemented
  └─→ evaluated      (monitoring window complete; outcome recorded)
        ├─→ [outcome: successful / neutral / inconclusive] — terminal
        └─→ [outcome: failed] → rolled_back

rolled_back          (terminal — downstream system signals rollback complete)
```

`suppressed`, `discarded`, `rejected`, `expired`, `evaluated`, and `rolled_back` are terminal states.

### 5.3 Re-proposal

A rejected, expired, or rolled-back recommendation may be re-proposed as a new recommendation instance if:
- New evidence has arrived since the prior terminal state (minimum: N new data points per domain threshold)
- The suppression window has elapsed (§10.3)
- The operator has not explicitly locked this recommendation type against re-proposal

Re-proposals are a new `recommendation_id` linked to the prior record via `supersedes_recommendation_id`.

---

## 6. Confidence Model

Every recommendation carries a confidence score (0.0–1.0) computed from eight inputs.

### 6.1 Inputs

| Input | Description | Effect on confidence |
|---|---|---|
| `evidence_count` | Number of independent signals supporting this recommendation | More independent signals → higher confidence. Diminishing returns above 10 independent signals. |
| `sample_size` | Total data points behind the strongest signal | Low sample_size caps maximum confidence. Below minimum threshold → recommendation discarded. |
| `time_window` | Duration over which the evidence was collected | Short windows reduce confidence for time-sensitive recommendations (e.g., a publish timing change based on 3 days of data). |
| `signal_strength` | Average z-score or effect size of the signals relative to baseline | Strong signals (large effect) drive confidence up. Weak signals (small effect, high variance) drive confidence down. |
| `consistency` | How consistently signals agree across independent sources | High consistency (all sources point the same direction) → confidence multiplier. Mixed signals → confidence reduction. |
| `counter_evidence` | Evidence pointing against the recommendation | Any counter_evidence depresses confidence. Strong counter_evidence (from the same domain with equivalent sample size) can prevent a recommendation from reaching the 0.70 floor. |
| `risk_level` | Inherent risk classification of the proposed change | High risk does not reduce confidence score but raises the confidence threshold required for auto-apply and affects OIS routing. |
| `confidence_in_estimate` | How confident the Learning Module is in the impact estimate (separate from the recommendation itself) | Low `confidence_in_estimate` is shown in the dashboard alongside the recommendation — the operator knows the impact prediction is uncertain even when the recommendation itself is well-evidenced. |

### 6.2 Computation

The confidence score is computed as:

```
base_confidence = f(evidence_count, sample_size, time_window)
signal_adjustment = signal_strength × consistency
counter_evidence_penalty = g(counter_evidence)

confidence_score = clamp(
  (base_confidence + signal_adjustment - counter_evidence_penalty),
  0.0, 1.0
)
```

The exact functions `f` and `g` are defined by the intelligence agent producing the recommendation (B6.5 §8). The Learning Module governance layer enforces the output constraints: no recommendation below 0.70 is surfaced, and all confidence scores are stored alongside the input components so they can be audited.

### 6.3 Confidence thresholds

| Threshold | Behavior |
|---|---|
| < 0.70 | Recommendation discarded — not surfaced. Stored in learning records for aggregate analysis only. |
| 0.70–0.74 | `passive_insight` — surfaced on Learning page only. Not shown on Home. Not routed to Approval System unless operator requests it. |
| 0.75–0.84 | `recommendation` — surfaced on Learning page. May appear on Home learning feed if composite_score meets threshold. |
| 0.85–1.00 | `recommendation` — eligible for Home recommendation cards (if composite_score ≥ 15 per OIS rules). |

Auto-apply thresholds are separate from surfacing thresholds and are per recommendation type (§4).

---

## 7. Risk Levels

Risk level is a classification attached to every recommendation, independent of confidence score. It determines authority requirements and governs whether auto-apply is possible.

### 7.1 LOW

Low-risk recommendations propose changes with narrow scope, clear reversibility, and no brand or safety implications.

Examples:
- Posting time adjustment (shift Thursday 18:00 UTC to 19:00 UTC)
- Title phrasing suggestion (increase question-format titles for MYSTERY pillar)
- Publish day adjustment within same week

**Auto-apply eligible:** Yes, with sufficient confidence and per channel's `auto_apply_threshold`.  
**Authority for human approval:** Operator+.  
**Rollback:** Immediate (just change the value back).

### 7.2 MEDIUM

Medium-risk recommendations propose changes with moderate scope, recoverable impact, and potential brand proximity.

Examples:
- Thumbnail style adjustment (shift from text-heavy to subject-focused compositions)
- Prompt guidance update (narrow scope, single agent, advisory change)
- Content pillar weight rebalance (e.g., MYSTERY 20% → 25%)
- Budget reallocation between equivalent-quality options
- Publishing cadence change (weekly → bi-weekly)

**Auto-apply eligible:** Only for budget_recommendation under specific conditions. Never for brand-adjacent changes.  
**Authority for human approval:** Operator+.  
**Rollback:** Within 1–2 episodes for most changes.

### 7.3 HIGH

High-risk recommendations propose changes that affect agent behavior, model selection, Channel Profile identity, or audience definition.

Examples:
- Model swap (Claude Haiku → Claude Sonnet for a specific agent)
- Provider swap (Seedance → Runway for video generation)
- Channel Profile audience update (change target_audience definition)
- Channel Profile content pillar addition or removal
- Workflow restructure (change stage ordering for an agent sequence)

**Auto-apply eligible:** Never.  
**Authority for human approval:** Operator+ for content pillar and audience changes. Admin+ for model swaps, provider swaps, and workflow restructures.  
**Rollback:** Requires Engineering Director or PMD to execute rollback. Minimum monitoring window of 10 production runs.

### 7.4 CRITICAL

Critical-risk recommendations propose changes that affect brand governance, safety rules, or compliance configuration. These are surfaced as recommendations but are not truly "recommendations" in the usual sense — they are proposals that require explicit operator/Admin decision and are never generated by the Learning Module's normal intelligence agents.

Examples:
- Brand rule modification (change tone_of_voice.forbidden_tones)
- Visual identity change (change color_palette or visual_identity.style_descriptor)
- Approval rule modification (change require_human_review or auto_publish)
- Auto-publish rule modification
- BGA threshold change

**Auto-apply eligible:** Never under any circumstances.  
**Authority:** Admin+ for brand and approval rule changes. Owner for auto-publish changes.  
**Important:** The Learning Module does NOT generate CRITICAL risk recommendations. CRITICAL risk fields are in the forbidden write set (§8.2). If a CRITICAL recommendation is needed, it comes from the operator directly via the Channel Profile edit, not from Learning.

---

## 8. Write-back Rules

### 8.1 The write-back constraint

The Learning Module has no direct write authority to any external system. Its only write operations are to its own memory: learning records, suppression rules, recommendation history, and evaluation records.

All proposed changes to other systems flow through this chain:

```
Learning Recommendation
  → submitted to OIS via submit_recommendation_to_ois()
    → OIS routes as recommendation or approval_request
      → Approval System collects decision
        → downstream system executes approved change
          → Learning Module records implementation
```

No shortcut exists. A recommendation that bypasses OIS has no governance record. A change that bypasses the Approval System has no authority enforcement. The chain is not optional.

### 8.2 Channel Profile: write-permitted and forbidden fields

**Write-permitted fields** — Learning may propose updates to these Channel Profile fields via channel_profile_recommendation. All proposals require Approval System routing:

| Field | Risk level | Minimum authority |
|---|---|---|
| `audience_demographics` (all sub-fields) | HIGH | Operator+ |
| `audience_interests` (order and content) | HIGH | Operator+ |
| `audience_personas` (weight adjustments to existing personas) | HIGH | Operator+ |
| `content_pillars[*].weight` (rebalancing existing pillars) | HIGH | Operator+ |
| `platform_strategy.platform_configs[*].day_of_week` | LOW | Operator+ (or auto with confidence ≥ 0.90) |
| `platform_strategy.platform_configs[*].time_of_day` | LOW | Operator+ (or auto with confidence ≥ 0.90) |
| `publishing_goals.engagement_target` | LOW | Operator+ |
| `publishing_goals.ctr_target` | LOW | Operator+ |

**Forbidden fields** — Learning may never propose updates to these fields. Any internally-generated signal that would require touching these fields is suppressed before reaching recommendation generation:

| Field category | Specific fields | Why forbidden |
|---|---|---|
| Brand identity | `tone_of_voice`, `brand_identity`, `forbidden_patterns` | Brand rules are human-governed. Learning cannot rewrite the rules by which its outputs are evaluated. |
| Visual identity | `visual_identity`, `color_palette`, `typography` | Visual brand rules are human-governed. BGA enforces them; Learning cannot change what BGA enforces. |
| Approval rules | `approval_rules`, `auto_publish`, `require_human_review`, `bga_enabled`, `compliance_required` | Safety-classified fields. No automated system may change approval governance. |
| Platform architecture | `enabled_platforms`, `primary_platform`, `cross_post_rules` | Platform-level decisions are strategic; affects audience reach at scale. Operator-only. |
| Content pillar identity | `content_pillars[*].pillar_name`, `content_pillars[*].description` | Adding or removing pillars changes channel identity. Pillar weights may be adjusted; pillar definitions may not. |
| Audience persona identity | `audience_personas[*].persona_name`, `audience_personas[*].description` | Creating or removing personas is a brand decision. Weight adjustments within existing personas are permitted. |
| Core identity | `channel_name`, `channel_description`, `niche`, `industry`, `language`, `region` | Core identity fields are immutable from the Learning Module's perspective. |

### 8.3 Engineering Director: write-permitted

Learning may propose changes that, if approved, are executed by Engineering Director:
- Module version upgrades (via workflow_recommendation → Admin+ approval → Engineering Director)
- Workflow stage configuration adjustments (via workflow_recommendation → Admin+ approval → Engineering Director)

Learning does not interact with Engineering Director directly. The Approval System signals Engineering Director after approval.

### 8.4 Provider/Model Director: signal-only

Learning sends signals to PMD. PMD decides whether to generate a swap proposal. Learning cannot submit a model_recommendation or provider_recommendation to OIS as a direct approval item — the signal goes to PMD first.

---

## 9. Recommendation Evaluation

Every implemented recommendation enters a monitoring window. At the end of the monitoring window, the Learning Module evaluates outcome by comparing post-implementation metrics against the pre-implementation baseline.

### 9.1 Evaluation inputs

| Metric | How measured |
|---|---|
| Performance before | Baseline computed from the evidence window used to generate the recommendation (e.g., CTR average across 10 pre-implementation episodes) |
| Performance after | Post-implementation average across the monitoring window (minimum N episodes per domain) |
| Statistical confidence | Whether the before/after difference is statistically significant given the sample sizes |
| Time stability | Whether improvement (or regression) is sustained across the monitoring window, or only visible in the first few episodes |
| Unexpected side effects | Metrics NOT targeted by the recommendation that moved in either direction during the monitoring window |
| Cost impact | Actual cost delta vs. estimated cost delta |
| Quality impact | Gate pass rate, override rate, and human approval rate during vs. before |
| Review impact | Time-to-review, rejection rate during monitoring window |

### 9.2 Outcome classification

| Outcome | Criteria |
|---|---|
| `successful` | Target metric improved by ≥ expected delta, sustained across ≥ 80% of monitoring window, no significant negative side effects |
| `neutral` | Target metric change within ±10% of baseline, no significant negative side effects |
| `failed` | Target metric regressed (worse than baseline), OR significant negative side effect detected, OR rollback was triggered |
| `inconclusive` | Insufficient data in monitoring window (too few episodes, external event contamination, or conflicting signals) |

### 9.3 Monitoring window sizes

| Recommendation type | Monitoring window |
|---|---|
| publishing_recommendation (time/day) | 6 publish cycles (e.g., 6 weeks if weekly cadence) |
| thumbnail_recommendation | 8 published episodes |
| content_recommendation | 6 published episodes |
| channel_profile_recommendation (audience) | 4 published episodes + 30 days of platform analytics |
| model_recommendation (via PMD) | 10 production runs |
| provider_recommendation (via PMD) | 10 production runs |
| budget_recommendation | 30 days post-implementation |
| workflow_recommendation | 10 production runs |
| prompt_recommendation | 8 production runs for affected agent |

### 9.4 Evidence chain separation

The recommendation evidence, the implementation decision, and the post-implementation evaluation are stored as three independent records. They share `recommendation_id` but are never merged:

- `RecommendationEvidence` — signals that supported the recommendation (collected BEFORE implementation)
- `RecommendationDecision` — the approval outcome (collected DURING governance flow)
- `RecommendationEvaluation` — the post-implementation measurement (collected AFTER implementation)

Evaluation metrics are never used as evidence for the same recommendation type in the same channel within 60 days of implementation. This prevents the evaluation outcome from becoming the evidence for a new recommendation that amplifies the first change.

---

## 10. Learning Memory

### 10.1 Memory types

The Learning Module maintains six memory types. These are carried over from B6.5 §4 and summarized here for the B5.7 governance context.

| Memory type | Scope | Contents | Retention |
|---|---|---|---|
| Short-term run memory | Single run or last 7 days | Recent failure modes, retry patterns, gate failures | 7 days, then promoted to channel memory if significant |
| Channel memory | Per channel, all episodes | Topic performance history, CTR/retention trends, audience profile, recurring failures, override history | Indefinite (individual signals expire per §2.1 metadata) |
| Template memory | Per template, all channels | Template strengths/weaknesses, gate pass rates by stage, model performance on template prompts | Indefinite |
| Factory-wide memory | All channels and agents | Cross-channel topic performance, cost trends, provider reliability, governance decisions | Indefinite |
| Provider/model memory | Per provider and model | Benchmark history, cost history, availability events, quality trends, failure modes | Indefinite |
| Agent memory | Per agent, per version | Performance trends, failure modes, prompt drift signals, override patterns | Indefinite per version; archived versions read-only |

### 10.2 Rejected recommendation memory

Every rejected recommendation is stored in a `LearningMemoryRecord` with type `rejection`. The record captures:
- Recommendation type and the specific proposal content
- Rejection reason category and free text
- Rejecting user (if human) or auto-expiry flag
- Timestamp and channel context

Rejected recommendation memory is the primary input to suppression rule evaluation (§10.3).

### 10.3 Suppression rules

A `SuppressionRule` is created when the Learning Module detects that a recommendation type is being repeatedly rejected or has been implemented and rolled back.

**Automatic suppression triggers:**

| Trigger | Suppression applied |
|---|---|
| Same recommendation type rejected 3 times in 90 days for the same channel | Suppress that recommendation type for that channel for 60 days |
| Recommendation implemented and rolled back (outcome = failed) | Suppress that recommendation type for that channel for 90 days |
| Recommendation expired (no decision) 2 times in a row | Suppress that recommendation type for 30 days |
| Operator explicitly marks recommendation type as "not useful for this channel" | Suppress indefinitely until operator removes the suppression |

**Suppression override:** A suppression rule can be bypassed by new evidence only if:
- New evidence arrives from an independent source not used in the suppressed recommendations
- The new evidence sample_size is ≥ 2× the minimum threshold for the domain
- The new evidence time window is entirely post-suppression (no overlap with the suppressed recommendation's evidence window)

All three conditions must be met simultaneously. If the suppression is bypassed by new evidence, the re-generated recommendation is flagged: "Note: a prior recommendation of this type was rejected/rolled back on [date]. New evidence has been evaluated independently."

### 10.4 Operator preference memory

When an operator rejects a recommendation and the rejection reason reveals a preference ("wrong timing", "disagree with recommendation", "evidence insufficient"), the Learning Module stores this as a preference signal on the channel's memory. Subsequent recommendations of the same type adjust their evidence requirements upward based on the rejection pattern.

This is not suppression — it is calibration. The Learning Module continues to observe the domain but raises its own internal threshold before generating another recommendation in that category. The effect is that the operator sees fewer, higher-confidence recommendations in areas where they have demonstrated skepticism.

---

## 11. Anti-Loop Protections

Six specific feedback loop failure modes are identified and protected against by architectural constraints.

### 11.1 Self-reinforcing feedback loop

**The loop:** Published content of type X performs well → Learning recommends more X → operator approves → more X is produced → all content converges to type X → audience develops fatigue → CTR drops for X → Learning detects the drop but attributes it to a new external variable → recommends even more X.

**Protection:**
- Content recommendations must include a `diversity_impact` field: how the recommended change affects the channel's content mix versus the content pillars defined in the Channel Profile.
- If a content_recommendation would result in any single pillar exceeding 70% of output, it is flagged as `diversity_risk: high` and requires Admin+ approval regardless of confidence.
- The channel memory tracks content mix as a time series. If any pillar exceeds 65% of output over a 60-day window, the Learning Module emits a `learning_signal` of type `content_diversity_alert` to OIS before generating any further content_recommendation.

### 11.2 Metric-metric tradeoff loops

**The loop (CTR vs. retention):** Optimize thumbnails for CTR → clickbait elements enter → retention drops → optimize content for retention → less "hook" content → CTR drops → optimize thumbnails again. Each optimization undermines the other.

**The loop (cost vs. quality):** Reduce cost by downgrading model → gate failures increase → more review cycles needed → total cost increases → reduce cost by downgrading model further.

**Protection:**
- No recommendation may propose optimizing a single metric if the Learning Module's evidence shows a statistically significant negative impact on one or more other key metrics (CTR, retention, gate pass rate, cost per published episode).
- Every recommendation must include a `secondary_metric_impact` section in its evidence package covering all key metrics, not only the target metric.
- If `secondary_metric_impact` shows any key metric regressing beyond its ±15% baseline tolerance, the recommendation requires explicit acknowledgement: the operator must confirm they have read the secondary impact before the Approval System allows approval.
- Cost recommendations may never propose changes that produce a quality delta < 0.

### 11.3 Repeated rejected recommendation

**The loop:** Learning generates recommendation A. Operator rejects it. Learning re-generates it at the next cadence because the underlying signal is still present. Operator rejects again. This repeats until the operator stops reviewing recommendations.

**Protection:** Suppression rules (§10.3). After 3 rejections of the same recommendation type in 90 days, the recommendation type is suppressed for 60 days. The suppression is visible on the Learning page — the operator can see that this recommendation type is currently suppressed and why. Suppression prevents recommendation spam without hiding the fact that suppression is active.

### 11.4 Using recommendation results as evidence

**The loop:** Recommendation A is implemented. Post-implementation metrics improve. These improved metrics are ingested as signals. A new Recommendation B is generated using the improved metrics as evidence. B is implemented. Metrics improve further. Eventually the signal chain is so far from the original state that no one can trace what changed and why.

**Protection:**
- Evidence chain separation (§9.4): `RecommendationEvidence` is sealed at the time the recommendation is generated. Evidence collected after the implementation start date cannot be used as evidence for any recommendation that supersedes or builds on the same recommendation_id.
- The 60-day post-implementation evidence lockout (§9.4) prevents evaluation metrics from becoming recommendation inputs.
- Every `LearningRecommendation` stores a `lineage` field: a list of recommendation_ids that preceded it in the same domain for the same channel. If the lineage chain exceeds 3 steps, a human review is required before the 4th recommendation in the chain is approved — even if confidence is high and risk is low. The operator reviews the full lineage before acting.

### 11.5 Overfitting to short-term or small-sample data

**The loop:** 3 episodes show unusual high CTR → Learning generates a thumbnail_recommendation with confidence 0.71 → operator approves (confidence is just above threshold) → changes implemented → next 10 episodes show CTR returning to baseline → Learning generates another recommendation to reverse the change.

**Protection:**
- Minimum sample sizes per domain (§3) are hard constraints, not soft guidance.
- The confidence computation explicitly penalizes short `time_window` values and caps maximum confidence at 0.75 when sample_size < 2× the domain minimum.
- The minimum stabilization windows (48h for retention curves, 24h for platform analytics) are enforced before signals are accepted.
- Recommendations generated from evidence below the 2× minimum threshold are labeled `early_signal` on the Learning page. Early signal recommendations require manual approval by Operator+ regardless of confidence score or `auto_apply_threshold` setting.

### 11.6 Monitoring window contamination

**The loop:** Recommendation A is implemented. During A's monitoring window, Recommendation B is also approved and implemented. Post-implementation metrics reflect both A and B. The evaluation for A incorrectly attributes B's effects to A. Both evaluations record `successful` outcomes. Neither rollback fires. The factory now has two changes that appeared successful individually but whose combined effect is unknowable.

**Protection:**
- Change isolation: when any recommendation is in `implemented` state (monitoring window active) for a given domain and channel, no second recommendation in the same domain for the same channel may enter the `approved` state. The second recommendation is held in `pending_approval` until the first monitoring window completes.
- Domain boundaries for change isolation: `thumbnail`, `content`, `publishing`, `model/provider`, `prompt/workflow`, `channel_profile`. Changes in different domains may overlap — the isolation applies only within the same domain.
- If the operator overrides the isolation hold and approves the second recommendation anyway, both monitoring windows are tagged `contaminated_window` and their evaluations are recorded as `inconclusive` regardless of observed metric direction.

---

## 12. Integration with OIS

### 12.1 Signal submission

The Learning Module submits signals to OIS via `submit_recommendation_to_ois()`. OIS is the sole routing layer — the Learning Module does not route to the dashboard, Notification System, or Approval System directly.

OIS classifies submitted signals into:

| OIS classification | Conditions | Routing destination |
|---|---|---|
| `passive_insight` | confidence 0.70–0.74, or `learning_signal` with no proposed change | Learning page only (no Home, no notification) |
| `recommendation` | confidence 0.75–0.84 | Learning page, possibly Home learning feed |
| `recommendation` (high) | confidence ≥ 0.85 | Learning page + eligible for Home recommendation cards |
| `approval_request` | Any recommendation where Approval System decision is required | Learning page + Approval System queue + Home Pending Actions |
| `suppressed_noise` | Signal matches OIS noise rules (same signal within deduplication window) | Audit trail only |

The Learning Module accepts OIS's routing decision. It does not re-submit a suppressed signal or challenge a `passive_insight` classification.

### 12.2 Confidence routing alignment

The Learning Module's confidence thresholds (§6.3) and OIS's routing logic are designed to align without duplication. The Learning Module computes confidence and attaches it to the signal. OIS reads confidence and applies its own routing rules. The two systems do not confer — OIS's decision is final.

### 12.3 OIS feedback to Learning Module

OIS does not send routing decisions back to the Learning Module as feedback. OIS routing is not learning data. However, the Learning Module monitors the suppression rate of its own signals (how often OIS classifies its signals as `suppressed_noise`) as a health metric. High suppression rates indicate either noisy signal generation or miscalibrated confidence thresholds.

### 12.4 Cadence enforcement

Learning signals are submitted to OIS on the cadence defined per intelligence agent in B6.5 §5. The Learning Module does not submit signals in real-time — it batches and submits on cadence. OIS deduplication windows (§9.6 of OIS design) enforce spacing. A recommendation submitted outside its cadence window is subject to deduplication and may be suppressed.

---

## 13. Integration with Approval System

The Approval System owns the decision. The Learning Module owns the evidence and the evaluation.

### 13.1 What the Learning Module provides to the Approval System

When a recommendation is routed to the Approval System, the Learning Module provides:
- The full `LearningRecommendation` record (what changes, why recommended)
- The `RecommendationEvidence` package (signal list, sample sizes, time window, confidence breakdown)
- The confidence score with input component breakdown
- The risk level and authority requirements
- The rollback plan (required for all recommendations that enter the Approval System)
- The estimated impact (quality delta, cost delta, latency delta, confidence_in_estimate)

The Approval System presents this to the operator via the PES C6 approval panel (B5.4 §6). The Learning Module's evidence package maps to the C6 sections:
- "What changes" → the specific proposed change
- "Why it was recommended" → evidence_summary + top 3 signals
- "Risk level" → risk_level badge
- "Affected entities" → target channel, agent, or Profile field
- "Rollback plan" → rollback_plan field
- "Evidence link" → deep link to full RecommendationEvidence record on Learning page

### 13.2 What the Approval System returns to the Learning Module

The Approval System calls `record_recommendation_decision()` when a decision is made:

| Decision | Effect on Learning Module |
|---|---|
| `approved` | Recommendation moves to `approved` state. Learning Module waits for implementation signal from downstream system. |
| `rejected` | Recommendation moves to `rejected` state. Rejection reason captured. Suppression rule evaluation triggered. Operator preference signal recorded. |
| `deferred` | Recommendation remains in `pending_approval`. Learning Module notes the deferral in the recommendation record. No suppression triggered for deferral. |

### 13.3 Implementation signal

After the Approval System signals the downstream system (Engineering Director, PMD, Channel Profile Manager), the downstream system signals the Learning Module when execution is complete:

```
record_recommendation_implementation(
  recommendation_id,
  implemented_by,          // "engineering_director" | "pmd" | "channel_profile_manager"
  implemented_at,
  implementation_notes,    // any deviation from the approved plan
  rollback_plan_confirmed  // boolean — downstream system confirms rollback path is ready
)
```

This moves the recommendation to `implemented` state and starts the monitoring window.

---

## 14. Integration with Review System

### 14.1 What Learning consumes from Review System

The Review System sends a `learning_signal` to OIS when certain patterns are detected. OIS routes these signals to the Learning Module.

| Review System signal | Learning Module domain | What it triggers |
|---|---|---|
| Repeated rejection pattern (3+ rejections, same reason_category, same channel, 30 days) | Human rejection patterns | Gate failure pattern analysis → may generate prompt_recommendation |
| High override rate for a gate type | Gate failure patterns | BGA or QA failure analysis → may generate prompt_recommendation or workflow_recommendation |
| High review age (median review age > 8h) | Workflow efficiency | Workflow analysis → may generate workflow_recommendation on review queue management |
| Rejection reason = `quality_insufficient` at rate > 20% of decisions | Prompt performance | Script Engine or VPD prompt analysis |
| Rejection reason = `brand_non_compliance` at rate > 10% | Gate failure patterns | BGA threshold analysis → surfaced as recommendation to review BGA threshold configuration (operator adjusts, not Learning) |

### 14.2 What Learning may propose based on Review data

Based on Review System signals, the Learning Module may generate:
- `prompt_recommendation` for agents whose outputs are frequently rejected (areas of concern, not specific text)
- `workflow_recommendation` for stage sequencing issues that are contributing to gate failures
- `quality_gate_adjustment` signal (surfaced as a passive_insight to the operator, never as a direct recommendation) — the operator decides whether to adjust quality gate thresholds

The Learning Module may NOT propose changes to gate thresholds directly. Gate threshold changes are CRITICAL risk (they change what constitutes a pass/fail for compliance and brand). The Learning Module surfaces the observation; the operator decides.

---

## 15. Integration with Provider/Model Director

### 15.1 Signal flow

The Learning Module identifies performance patterns but does NOT own the swap proposal. The flow is:

```
Learning Module detects performance signal
  → creates model_recommendation or provider_recommendation (signal type)
  → submitted to OIS
  → OIS routes to PMD (not to Approval System directly)
    → PMD evaluates the signal against its own benchmark data
      → PMD generates swap proposal (or declines)
        → if PMD generates swap proposal:
          → swap proposal routed through Approval System (model_swap type)
```

The Learning Module's role ends when the signal is submitted to OIS. PMD decides whether the signal warrants a swap proposal. If PMD declines, the Learning Module records the outcome and applies evidence requirements for future signals of the same type.

### 15.2 What Learning sends to PMD

```
provider_model_signal {
  signal_type:        "performance_degradation" | "cost_opportunity" | "quality_improvement"
  target_type:        "model" | "provider"
  current_model_id or current_provider_id,
  evidence_type:      "gate_pass_rate" | "quality_score" | "cost_per_output" | "latency"
  observed_value,
  baseline_value,
  time_window_days,
  sample_size,
  affected_agent_id,
  affected_channel_id
}
```

PMD may request additional evidence from the Learning Module before generating a swap proposal. The Learning Module responds to evidence requests via `get_learning_memory()` for the relevant domain.

---

## 16. Data Contracts

### 16.1 LearningSignal

```
LearningSignal {
  signal_id:                string
  source:                   string          // source system
  source_entity_id:         string          // episode_id, channel_id, agent_id, etc.
  signal_type:              string          // typed classification per domain
  signal_scope:             enum(run, episode, channel, factory, provider, agent)
  domain:                   enum(content_performance, title_performance, thumbnail_performance,
                                 retention_performance, publishing_timing, platform_performance,
                                 audience_behavior, gate_failure_patterns, human_rejection_patterns,
                                 model_provider_performance, prompt_performance,
                                 cost_efficiency, workflow_efficiency)
  value:                    object          // domain-specific structured value
  measured_at:              timestamp       // when the underlying event occurred
  ingested_at:              timestamp       // when the Learning Module received it
  confidence:               float           // how reliable this measurement is (0.0–1.0)
  sample_size:              int
  min_sample_threshold_met: boolean
  expires_at:               timestamp       // signals expire; stale signals cannot anchor recommendations
  supersedes_signal_id:     string | null   // if this is an update to a prior signal
}
```

### 16.2 LearningRecommendation

```
LearningRecommendation {
  recommendation_id:          string
  recommendation_type:        enum(content_recommendation, title_recommendation,
                                    thumbnail_recommendation, publishing_recommendation,
                                    prompt_recommendation, model_recommendation,
                                    provider_recommendation, channel_profile_recommendation,
                                    workflow_recommendation, budget_recommendation)
  source_agent:               string          // intelligence agent that generated this
  source_learning_record_ids: string[]        // learning records this is based on
  channel_id:                 string
  target_type:                string          // what entity this affects
  target_id:                  string
  target_field_path:          string | null   // for channel_profile_recommendation, the field
  what_changes:               string          // plain-language description
  why_recommended:            string          // evidence summary
  risk_level:                 enum(LOW, MEDIUM, HIGH, CRITICAL)
  estimated_impact: {
    primary_metric:           string
    primary_delta:            float
    secondary_metric_impact:  map<metric_name, float>
    cost_delta_usd_monthly:   float | null
    confidence_in_estimate:   float
  }
  rollback_plan:              string          // required for all recommendations
  lineage:                    string[]        // prior recommendation_ids in same domain/channel
  diversity_impact:           string | null   // for content_recommendations
  early_signal_flag:          boolean         // true if sample_size < 2× domain minimum
  status:                     enum(generated, suppressed, pending_approval, approved,
                                    rejected, expired, implemented, evaluated, rolled_back)
  confidence_score:           float
  confidence:                 RecommendationConfidence
  created_at:                 timestamp
  expires_at:                 timestamp       // default 30 days from creation
  decided_at:                 timestamp | null
  implemented_at:             timestamp | null
  evaluated_at:               timestamp | null
  supersedes_recommendation_id: string | null
}
```

### 16.3 RecommendationEvidence

```
RecommendationEvidence {
  evidence_id:              string
  recommendation_id:        string
  evidence_signals:         LearningSignal[]  // signals collected BEFORE implementation
  evidence_episodes:        string[]          // episode_ids used as evidence
  evidence_sealed_at:       timestamp         // evidence cannot be modified after this
  primary_metric_baseline:  float             // pre-implementation baseline for primary metric
  secondary_metric_baselines: map<metric_name, float>  // pre-implementation baselines
  evidence_time_window_days: int
  total_sample_size:        int
  conflicting_signals:      LearningSignal[]  // counter_evidence signals
}
```

### 16.4 RecommendationConfidence

```
RecommendationConfidence {
  recommendation_id:        string
  evidence_count:           int
  sample_size:              int
  time_window_days:         int
  signal_strength:          float             // average effect size across signals
  consistency:              float             // 0.0–1.0; how consistently signals agree
  counter_evidence_score:   float             // 0.0–1.0; strength of opposing signals
  risk_level:               enum(LOW, MEDIUM, HIGH, CRITICAL)
  confidence_in_estimate:   float             // confidence in the impact estimate
  confidence_score:         float             // final computed confidence (0.0–1.0)
  computed_at:              timestamp
}
```

### 16.5 RecommendationDecision

```
RecommendationDecision {
  decision_id:              string
  recommendation_id:        string
  approval_id:              string            // Approval System record
  decision:                 enum(approved, rejected, deferred)
  decided_by:               string            // user_id or "auto_policy"
  decided_at:               timestamp
  rejection_reason_category: string | null
  rejection_reason_text:    string | null
  modification_note:        string | null     // if operator modified params before approving
}
```

### 16.6 RecommendationEvaluation

```
RecommendationEvaluation {
  evaluation_id:            string
  recommendation_id:        string
  monitoring_window_start:  timestamp
  monitoring_window_end:    timestamp
  contaminated_window:      boolean           // true if another change was active simultaneously
  primary_metric_before:    float
  primary_metric_after:     float
  primary_metric_delta:     float
  primary_metric_delta_significant: boolean
  secondary_metric_results: map<metric_name, {before, after, delta, significant}>
  cost_delta_actual:        float | null
  quality_gate_rate_change: float | null
  human_approval_rate_change: float | null
  unexpected_side_effects:  string | null
  outcome:                  enum(successful, neutral, failed, inconclusive)
  outcome_rationale:        string
  evaluated_at:             timestamp
  evaluator:                string            // "auto" or user_id if manually evaluated
}
```

### 16.7 LearningMemoryRecord

```
LearningMemoryRecord {
  memory_record_id:         string
  memory_type:              enum(run_short_term, channel_memory, template_memory,
                                  factory_wide, provider_model, agent, rejection, preference)
  channel_id:               string | null
  agent_id:                 string | null
  entity_id:                string            // the specific entity this record is about
  domain:                   string            // learning domain this record belongs to
  content:                  object            // structured memory content (domain-specific)
  derived_from:             string[]          // signal_ids or other memory_record_ids
  confidence:               float
  created_at:               timestamp
  last_reinforced_at:       timestamp         // last time new evidence confirmed this memory
  expires_at:               timestamp | null  // null = indefinite
}
```

### 16.8 SuppressionRule

```
SuppressionRule {
  suppression_id:           string
  recommendation_type:      string            // which recommendation type is suppressed
  channel_id:               string            // which channel this suppression applies to
  trigger:                  enum(repeated_rejection, rollback, repeated_expiry, operator_set)
  trigger_details:          string            // description of what triggered the suppression
  rejection_count:          int | null        // if trigger = repeated_rejection
  suppressed_at:            timestamp
  suppression_ends_at:      timestamp | null  // null = indefinite (operator_set)
  override_evidence_threshold: object         // min evidence required to bypass suppression
  override_active:          boolean           // true if suppression has been bypassed by new evidence
  created_by:               string            // "learning_module" or user_id
}
```

---

## 17. API Contracts

### 17.1 submit_learning_signal()

Called by: Intelligence agents (B6.5), Review System, Analytics Layer, Run OS.

```
submit_learning_signal(
  source,
  source_entity_id,
  signal_type,
  signal_scope,
  domain,
  value,
  measured_at,
  confidence,
  sample_size
) → { signal_id, ingested: boolean, rejection_reason: string | null }
```

Rejects: signals below domain minimum sample threshold; duplicate signals within deduplication window; signals with `measured_at` outside the valid ingestion window (too old or future-dated).

### 17.2 generate_recommendations()

Called by: Intelligence agents (on cadence), Dashboard (on demand with force=true).

```
generate_recommendations(
  agent_capability,
  scope,
  scope_id,
  force
) → { recommendations_generated, recommendation_ids, discarded_count, suppressed_count }
```

Does not modify any external system. Idempotent within cadence window.

### 17.3 score_recommendation()

Called by: Learning Module internally after recommendation generation.

```
score_recommendation(
  recommendation_id
) → RecommendationConfidence
```

### 17.4 suppress_recommendation()

Called by: Learning Module (automatically per suppression rules), operator (manual suppression).

```
suppress_recommendation(
  recommendation_id,
  reason,
  suppressed_by            // "learning_module" | user_id
) → { suppressed: boolean }
```

### 17.5 submit_recommendation_to_ois()

Called by: Learning Module (after scoring, if confidence ≥ 0.70).

```
submit_recommendation_to_ois(
  recommendation_id,
  recommendation_type,
  confidence_score,
  risk_level,
  channel_id,
  what_changes,
  why_recommended,
  estimated_impact,
  rollback_plan,
  early_signal_flag
) → { signal_id, ois_classification, routed_to }
```

### 17.6 record_recommendation_decision()

Called by: Approval System (after decision).

```
record_recommendation_decision(
  recommendation_id,
  approval_id,
  decision,                // approved | rejected | deferred
  decided_by,
  decided_at,
  rejection_reason_category,
  rejection_reason_text
) → RecommendationDecision
```

Internally: if rejected, evaluates suppression rule creation. If approved, waits for implementation signal.

### 17.7 evaluate_implemented_recommendation()

Called by: Learning Module (at monitoring window end, automatically).

```
evaluate_implemented_recommendation(
  recommendation_id,
  monitoring_window_start,
  monitoring_window_end,
  contaminated
) → RecommendationEvaluation
```

If `outcome = failed`, sends rollback signal to the downstream system that executed the change.

### 17.8 get_learning_history()

Called by: Dashboard, PMD (for evidence requests), Approval System (for evidence package).

```
get_learning_history(
  channel_id,
  domain,
  recommendation_type,
  status,
  date_from,
  date_to,
  page,
  page_size
) → LearningRecommendation[]
```

### 17.9 get_learning_memory()

Called by: Intelligence agents, Dashboard, PMD.

```
get_learning_memory(
  memory_type,
  channel_id,
  entity_id,
  domain
) → LearningMemoryRecord[]
```

### 17.10 generate_learning_report()

Called by: Dashboard (on demand or on monthly cadence).

```
generate_learning_report(
  channel_id,
  window_days            // default 30
) → {
  generated_at,
  channel_id,
  window_days,
  signals_ingested,
  recommendations_generated,
  recommendations_by_status: map<status, int>,
  recommendations_by_type: map<type, int>,
  auto_applied_count,
  human_approved_count,
  rejection_rate,
  implementation_rate,
  outcome_distribution: map<outcome, int>,
  rollback_count,
  avg_confidence_score,
  active_suppression_rules: SuppressionRule[],
  monitoring_active_count,
  top_performing_implementation: LearningRecommendation | null,
  worst_performing_implementation: LearningRecommendation | null
}
```

### 17.11 get_active_suppression_rules()

Called by: Dashboard (Learning page suppressed recommendations section), operator review.

```
get_active_suppression_rules(
  channel_id
) → SuppressionRule[]
```

---

## 18. Dashboard Visibility

The Learning page is the primary operator surface for the Learning Module.

### 18.1 Page structure

```
LEARNING  /  [channel selector]
──────────────────────────────────────────────────────────────────────

[1] ACTIVE RECOMMENDATIONS  (badge: n)
    Recommendations in pending_approval, ready for operator review

[2] PENDING APPROVALS  (badge: n)
    Items currently in Approval System — link to Approval System panels

[3] MONITORING — IMPLEMENTED  (badge: n)
    Approved and implemented; monitoring window active

[4] EVALUATION RESULTS  (last 30 days)
    Completed evaluations with before/after comparison

[5] REJECTED RECOMMENDATIONS  (last 90 days)
    Rejected items; suppression status shown

[6] IMPLEMENTED — HISTORY
    All implemented recommendations (paginated)

[7] SUPPRESSED RECOMMENDATIONS
    Active suppression rules; operator can review and remove suppressions

[8] CHANNEL LEARNING HISTORY
    Timeline view of all learning events for this channel

[9] LEARNING CONFIDENCE
    System health: signal ingestion, intelligence agent status, confidence distribution

[10] LEARNING REPORT  [Generate Report]
     Monthly or on-demand summary
```

### 18.2 Active Recommendations section

Each recommendation card shows:
- Recommendation type badge
- Target (channel, agent, or Profile field)
- Plain-language summary of what changes and why
- Confidence score (visually prominent)
- Risk level badge
- Sample size and evidence episodes (collapsible)
- Estimated impact (primary metric + secondary metric impacts)
- Evidence link (opens RecommendationEvidence detail)
- Age (how long pending)
- `early_signal` warning if flagged
- [View full recommendation] → opens full evidence package and approval panel

For recommendations requiring Approval System decision: the full PES C6 approval panel is embedded inline (same as Review page's inline approval panels).

For passive_insight items (confidence 0.70–0.74): shown in a lower-emphasis section labeled "Low confidence observations" with a note: "Not yet ready for action — monitoring for more evidence."

### 18.3 Suppressed Recommendations section

Shows all active SuppressionRules for the selected channel:

```
SUPPRESSED RECOMMENDATIONS

  ■ thumbnail_recommendation                    suppressed since 2026-05-14
    Reason: rejected 3 times in 90 days
    Latest rejection: "Evidence insufficient" (2026-05-14)
    Suppression ends: 2026-07-14  (32 days remaining)
    Evidence threshold to override: 20 episodes + 10,000 impressions (post-suppression)
    [Remove Suppression]  ← Admin only

  ■ publishing_recommendation (cadence)         suppressed indefinitely
    Reason: operator-set ("not useful for this channel")
    Set by: gawad  on 2026-04-20
    [Remove Suppression]  ← Admin only
```

### 18.4 Learning Confidence panel

```
LEARNING CONFIDENCE

  Signal Sources
  ■ YouTube Analytics       last ingested: 6h ago           ✓ healthy
  ■ Acceptance Audit        last ingested: 2h ago           ✓ healthy
  ■ Review System           last ingested: 1h ago           ✓ healthy
  ■ Cost Ledger             last ingested: 12h ago          ✓ healthy

  Intelligence Agents
  ■ Run Analyst             last run: 2h ago                ✓ healthy
  ■ Audience Analyst        last run: 18h ago               ✓ healthy
  ■ Topic Intelligence      last run: 3 days ago            ✓ on cadence
  ■ Thumbnail Intelligence  last run: 4 days ago            ✓ on cadence
  ■ Cost Intelligence       last run: 11h ago               ✓ healthy
  ■ Factory Strategist      last run: 28 days ago           ✓ on cadence (monthly)

  Recommendation Health
    Generated (30d):        12
    Confidence ≥ 0.85:       7   (58%)
    Confidence 0.75–0.84:    3   (25%)
    Confidence 0.70–0.74:    2   (17%)
    Discarded (< 0.70):      4
    Suppressed:              2
    OIS suppressed_noise:    1

  Rollback Rate (90d):      0 / 5 implemented  (0%)
  Approval Rate (90d):      4 / 6 decided      (67%)
```

### 18.5 Role-based visibility

| Section | Editor | Operator | Admin | Owner |
|---|---|---|---|---|
| Active Recommendations | ✓ (read only) | ✓ (can approve/reject within authority) | ✓ | ✓ |
| Pending Approvals | ✓ (read only) | ✓ | ✓ | ✓ |
| Monitoring / Evaluation | ✓ (read only) | ✓ (read only) | ✓ | ✓ |
| Rejected / History | ✓ (read only) | ✓ (read only) | ✓ | ✓ |
| Suppressed Recommendations | — | ✓ (read only) | ✓ (can remove suppressions) | ✓ |
| Learning Report (generate) | — | ✓ | ✓ | ✓ |
| Learning Confidence | — | ✓ (read only) | ✓ | ✓ |

---

## 19. Risk Register

### Risk 1 — Self-reinforcing feedback loop

**Failure mode:** The factory publishes more of what is learning-recommended, recommendations become based on what was recently published, and the recommendation engine is effectively predicting what it already decided to produce. The factory optimizes itself into a single content archetype.

**Mitigation:** Content diversity protection (§11.1). Any recommendation that would push a content pillar above 70% of output is blocked and requires Admin+ approval. Content mix is tracked as a time series; if any pillar exceeds 65% over 60 days, a `content_diversity_alert` signal is raised before the next content_recommendation is generated. The diversity invariant is checked before content_recommendation submission to OIS.

---

### Risk 2 — Overfitting to small sample sizes

**Failure mode:** Three consecutive episodes show strong CTR (anomaly, not trend). The Learning Module generates a recommendation at 0.71 confidence. The operator approves. The next 10 episodes revert to baseline. The Learning Module generates a reverse recommendation.

**Mitigation:** Minimum sample sizes per domain are hard constraints that cap maximum confidence at 0.75 when below 2× minimum (§11.5). `early_signal` flag is shown visibly in the dashboard. Early signal recommendations require manual approval by Operator+ regardless of `auto_apply_threshold`. The 48-hour stabilization window for analytics data prevents early noisy signals from entering the evidence base.

---

### Risk 3 — Optimizing the wrong metric

**Failure mode:** CTR improves. Retention collapses (clickbait). Or: cost per episode reduces. Gate failure rate rises (cheap model produces lower-quality output that fails BGA). The optimized metric is visible; the damaged metric is not immediately surfaced in the recommendation flow.

**Mitigation:** Every recommendation includes a `secondary_metric_impact` section (§11.2). No recommendation that shows a secondary metric regressing beyond ±15% baseline tolerance may be approved without explicit operator acknowledgement of the secondary impact. Auto-apply is blocked for any recommendation with secondary metric regression. Cost recommendations must show quality delta ≥ 0.

---

### Risk 4 — Recommendation spam

**Failure mode:** The Learning Module generates a recommendation every week. Operators stop reading them. A critical recommendation is buried in a stack of low-priority suggestions.

**Mitigation:** Suppression rules (§10.3) prevent repeated recommendations. OIS deduplication windows enforce spacing (same signal type has a minimum re-submission interval). Confidence thresholds ensure only well-evidenced recommendations reach the dashboard. The cadence constraints per intelligence agent (B6.5 §5) prevent daily-frequency recommendations — no intelligence agent runs more frequently than daily, and most run weekly or monthly. Home recommendation cards are capped at 3 (OIS §9.5).

---

### Risk 5 — Ignored rejection history

**Failure mode:** An operator has rejected the same type of thumbnail recommendation 3 times. The Learning Module generates it again because a new episode showed a brief CTR improvement matching the rejected recommendation's pattern.

**Mitigation:** Suppression rules enforce a 60-day suppression after 3 rejections. The suppression override requires new independent evidence meeting 3 strict criteria simultaneously (§10.3). If the suppression is bypassed, the re-generated recommendation is labeled with the suppression history so the operator has explicit context before acting.

---

### Risk 6 — Unsafe write-back to Channel Profile

**Failure mode:** A channel_profile_recommendation is submitted and approved. The implementation writes to `forbidden_patterns.phrases` directly. The brand vocabulary constraints are now controlled by the learning system.

**Mitigation:** The forbidden field list (§8.2) is enforced at the API boundary of the Channel Profile Manager (`apply_profile_update()`). Any attempt to write to a forbidden field is rejected regardless of whether an approval exists. The approval record cannot override the architectural constraint. The Learning Module's signal generation also has an upstream check: signals that would require touching forbidden fields are suppressed before they reach recommendation generation.

---

### Risk 7 — Model/provider confusion

**Failure mode:** The Learning Module emits a model_recommendation signal that PMD receives. PMD interprets it as a strong swap proposal signal and submits a model swap to the Approval System. The operator approves the swap based on Learning Module confidence, not realizing PMD's additional benchmarking was minimal. The swap produces degraded output.

**Mitigation:** The Learning Module's model/provider signals are typed as `provider_model_signal` — not `recommendation`. PMD is responsible for generating the swap proposal from its own evaluation process. PMD must cite its own benchmark evidence in the swap proposal, separate from the Learning Module's signal. The Approval System's approval panel shows PMD as the source of the swap proposal — not the Learning Module. This makes the evidence chain transparent to the operator.

---

### Risk 8 — Learning reduces brand consistency

**Failure mode:** The Learning Module recommends audience_interests reordering based on 5 episodes. The Channel Profile is updated. The Research Director's topic scoring changes. The content pillar mix shifts. Episodes diverge from the original brand identity over 3 months without a single large change that the operator would have reviewed carefully.

**Mitigation:** Channel Profile write-permitted fields (§8.2) are HIGH risk, always requiring Operator+ approval. Every channel_profile_recommendation is presented with its lineage (§16.2 `lineage` field) showing prior profile changes in the same domain. If the lineage chain exceeds 3 steps in the same domain within 180 days, Admin+ review is required for the 4th step. The `profile_version` is captured in every run record, creating a traceable history of which profile version governed each episode's production — the operator can see when profile changes coincide with content drift.

---

### Risk 9 — False confidence

**Failure mode:** The confidence score is 0.87 on a thumbnail recommendation. The operator approves without reading the evidence because 0.87 feels authoritative. The recommendation was based on 11 episodes (barely above the 10-episode minimum). The effect size was small. The counter_evidence section contains 2 competing signals that the confidence function discounted because they had smaller sample sizes.

**Mitigation:** The confidence breakdown (§6.2) is displayed in the approval panel, not hidden behind an evidence link. The operator sees: `evidence_count: 2`, `sample_size: 11`, `signal_strength: 0.34 (moderate)`, `consistency: 0.71`, `counter_evidence: 2 signals (not shown due to sample size)`. Counter_evidence is shown even when it is discounted. The `confidence_in_estimate` is shown separately from `confidence_score` so the operator knows the impact estimate may be uncertain even when the recommendation direction is well-evidenced. For `early_signal` recommendations (sample_size < 2× minimum), a banner explicitly states that the evidence is below the normal threshold.

---

## 20. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.275 — OIS | Learning submits all signals and recommendations to OIS via `submit_recommendation_to_ois()`. OIS classifies and routes. Learning accepts OIS routing decisions without challenge. OIS deduplication windows enforce cadence spacing. |
| B5.35 — Notification System | Learning does not call NS directly. OIS routes qualifying learning signals to NS (e.g., `learning_recommendation` notification type for digest delivery). |
| B5.4 — Approval System | Approval System owns decision authority. Learning provides the evidence package; Approval System presents it via PES C6. Approval System signals Learning Module on decision via `record_recommendation_decision()`. |
| B5.5 — Review System | Review System sends repeated rejection patterns and gate failure patterns to OIS as `learning_signal`. Learning Module receives these via OIS routing. Rejection data feeds human_rejection_patterns and gate_failure_patterns domains. |
| B5.6 — Channel Profile | Channel Profile defines write-permitted fields and forbidden fields for Learning. Learning's channel_profile_recommendations target only write-permitted fields. Channel Profile Manager's `apply_profile_update()` enforces the forbidden field boundary. |
| B6.5 — Learning Intelligence Layer | B6.5 intelligence agents produce the signals and raw recommendations. B5.7 is the governance container: confidence scoring, suppression rules, write-back enforcement, OIS routing, and evaluation. B6.5 without B5.7 is unconstrained signal generation. B5.7 without B6.5 is an empty governance shell. |
| B4.9 — Engineering Director | Approved workflow_recommendations are signaled to Engineering Director for execution. Engineering Director signals Learning Module when implementation is complete (`record_recommendation_implementation()`). |
| Provider/Model Director | Learning sends provider_model_signals to PMD. PMD generates swap proposals independently. Learning records whether PMD acted on each signal and uses this as calibration for future signal generation in the model/provider performance domain. |
