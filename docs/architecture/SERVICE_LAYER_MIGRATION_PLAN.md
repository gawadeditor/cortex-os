# Service Layer Incremental Migration Plan

**2026-07-26 phase-numbering note**: `ROADMAP.md` was renumbered after
this document was written. Phase references below use the pre-renumber
scheme (Phase 3 = Analytics, Phase 4 = Learning, Phase 6 = Multi-Channel
Platform). Current numbering: Analytics = Phase 4, Learning = Phase 5,
Multi-Channel Platform = Phase 8. Two new phases were inserted before
Multi-Channel: Phase 3 (Production Hardening) and Phase 6 (Factory
Capability Evolution, `DECISION_LOG.md` ADR-012). This note is a pointer,
not a rewrite — treat `ROADMAP.md` as authoritative for phase numbers,
this document for the Service-layer analysis itself, which is unaffected
by the renumbering.

Produced 2026-07-25, in response to explicit correction on the Architecture
Alignment Report: the Core → Service → Agent boundary defined by
`SERVICE_MODEL.md` / `AGENT_MODEL.md` / `EXECUTION_MODEL.md` is not a
deferred Phase 6 item. It is a standing architectural boundary that new
and touched code should progressively converge toward now. This document
is the incremental plan for that convergence — not a rewrite proposal.

This also resolves Required Migration #2 from
`docs/architecture/CORTEX_ALIGNMENT_REPORT.md` ("decide how the missing
Service layer is interpreted"): the answer is (b), a real Service layer,
built incrementally rather than declared and left un-built.

Documentation-only. No code is changed by this plan.

---

## Governing constraint

Two rules apply simultaneously and neither overrides the other:

> **Architecture defines HOW.** Converge toward Core → Service → Agent
> whenever code in that area is touched for a real reason. Never touch
> code solely to relocate it into a Service.

> **The Roadmap defines WHAT and WHEN.** Implementation priority always
> follows `ROADMAP.md`'s sequence. A capability is never pulled forward
> because building it as a Service is easier or lower-risk than converging
> existing code — see "Two independent axes" below.

`DESIGN_PRINCIPLES.md` (Progressive Evolution: "prefer continuous
improvement over large disruptive redesigns") supports the first rule and
rules out a scheduled migration sprint for either axis.

Two consequences follow from the architecture rule:

1. **New work is built Service-shaped from the start.** This costs
   nothing extra — it's a design choice at the moment of first writing
   the code, not a migration.
2. **Existing work converges opportunistically.** The next real bug fix
   or feature touching a given agent module is the trigger to introduce
   a thin Service boundary around that module's domain — not before.

Nothing here proposes a deadline, a tracking ticket, or a "do this next"
task. It defines *where the seams are* and *what to do when a touch
happens there*, so that convergence is consistent instead of ad hoc.

---

## Where the seam already exists

`hypogeum/pipeline.py::run_pipeline()` (L380) calls ten private wrapper
functions in the frozen stage order, one per pipeline stage:

```
_run_executive_producer        L1325
_run_research_director         L1358
_run_narrative_architect       L1390
_run_script_engine             L1421
_run_thumbnail_strategy        L1454
_run_visual_package_director   L1502
_run_thumbnail_director        L1572
_run_audio_agent               L1614
_run_assembly_agent            L1642
_run_publishing_agent          L1880
```

(Plus supporting stages already isolated the same way: `_run_archive_agent`,
`_run_compliance_agent`, `_run_qa_agent`, `_run_budget_authorization`,
`_run_cost_ledger`, `_run_asset_archive`.)

This is the important finding: **the Stage boundary from `WORKFLOW_MODEL.md`
already exists structurally** as these `_run_*` functions. Each one already
does exactly what a Stage should do — take validated prior output, invoke
one thing, record start/complete/failed, return a validated package. What's
missing is only the layer *inside* each wrapper: today each wrapper calls
an agent module function (e.g. `run_research_director()`) directly; a
Service would sit between the two.

This is why the convergence is low-risk: introducing a Service means
changing what a `_run_*` wrapper calls internally, not changing
`run_pipeline()`'s call order, not changing the frozen stage sequence in
CLAUDE.md, and not changing any agent module's actual logic. The wrapper's
external contract (validated package in, validated package out, same
`_record_stage_*` bookkeeping) stays identical.

---

## Target Service mapping for this codebase

Using `SERVICE_MODEL.md`'s own categories, mapped onto what already exists:

| Service | Category | Current agent modules it would own |
|---|---|---|
| Discovery Service | Production | `hypogeum/topic_discovery.py`, `discovery/topic_ideator.py`, `agents/discovery_director.py` |
| Research Service | Production | `agents/research_director.py` |
| Script Service | Production | `agents/narrative_architect.py`, `agents/script_engine.py` |
| Visual Service | Production | `agents/thumbnail_strategy.py`, `agents/visual_package_director.py`, `agents/scene_planner.py`, `agents/entity_continuity_manager.py` |
| Thumbnail Service | Production | `agents/thumbnail_director.py` and its ~12 sub-agents (composition_planner, compositor, prompt_writer, qa, etc.) |
| Voice Service | Production | `agents/audio_agent.py`, `agents/voice_generation_operator.py`, `agents/audio_qa.py` |
| Assembly Service | Production | `agents/assembly_agent.py`, `agents/assembly_intelligence.py`, `agents/video_renderer.py`, `agents/timeline_builder.py` |
| Publishing Service | Operational | `agents/publishing_agent.py`, `hypogeum/publishing/youtube.py`, `hypogeum/publishing/meta.py` |
| Scheduling Service | Operational | `hypogeum/scheduler.py`, `hypogeum/queue.py`, `hypogeum/worker_integration.py` |
| Analytics Service | Intelligence | **does not exist yet — Phase 3** |
| Learning Service | Intelligence | **does not exist yet — Phase 4** |
| Provider Service | Platform | `hypogeum/muapi.py`, `hypogeum/openrouter.py`, `hypogeum/elevenlabs.py`, `hypogeum/perplexity.py` (already close to compliant — see Alignment Report's "Fully Aligned Areas") |
| Secrets Service | Platform | `hypogeum/credential_manager.py` (already close to compliant, reinforced by this session's OAuth fix) |
| Finance Service | Platform | `hypogeum/finance.py`, `agents/finance_agent.py` |

This table is the reference for "which Service does this touch belong to"
the next time any of the left-hand modules changes for a real reason.

---

## Two independent axes, kept separate

**Architecture answers HOW. The Roadmap answers WHAT and WHEN.** The list
below is ordered by roadmap sequence, not by ease of Service extraction.
Analytics and Learning are architecturally convenient places to introduce
a Service — no legacy code, no regression surface — but that convenience
never promotes them ahead of Runtime work. They are built when the
Roadmap reaches them, exactly as it would specify regardless of this plan;
this plan only fixes *how* they're built once that point is reached.

Current engineering priority, per Roadmap sequence (unchanged by this
plan):

1. Complete reliable Autonomous Runtime.
2. Achieve unattended end-to-end production.
3. Strengthen monitoring and recovery.
4. Complete analytics feedback loops (Phase 3).
5. Expand Factory Learning and Channel Learning (Phase 4).

Items 1–2 are substantially achieved as of this session's MAR repair (H1/H2)
and the first fully autonomous end-to-end production run — `CURRENT_STATE.md`
still needs updating to reflect this (Alignment Report Required Migration
#1, not yet done). Item 3 is the current engineering frontier. Items 4–5
are not to be started early merely because they are easier to build as
Services.

### When item 4 (Analytics) is reached: build it as a Service

`hypogeum/services/analytics_service.py` with a stable
`execute(context) -> AnalyticsResult` contract, internally calling
`hypogeum/youtube_analytics.py` and whatever cross-platform collectors
Phase 3 needs, costs nothing beyond what building Phase 3 already costs
whenever the Roadmap reaches it. It becomes the first real, working
example of the Service Model in this codebase, with zero regression risk
to the production pipeline because it touches no existing code path. This
is a statement about *shape*, not *timing* — it does not pull Phase 3
earlier.

### When item 5 (Learning) is reached: same logic, one phase later

Same reasoning as Analytics: new code, no legacy to migrate, natural
consumer of the Analytics Service's output via the Core (never
Service-to-Service directly, per `SERVICE_MODEL.md`'s Communication rule).

### In parallel, at any time: opportunistic extraction from existing `_run_*` wrappers

No specific module is scheduled. The convention going forward: when a
`_run_*` wrapper (or the agent module(s) it calls) is genuinely touched
for a bug fix or feature — not for its own sake — introduce a thin Service
module for that row of the mapping table above, if one doesn't already
exist, and move the wrapper's call from "invoke agent module directly" to
"invoke Service, which invokes the agent module." The wrapper keeps doing
its own bookkeeping (`_record_stage_start/complete/failed`) exactly as
today; only the one call inside it changes target.

Natural first candidates by touch likelihood, not by schedule: Publishing
Service (already had two real defects fixed this session —
`credential_manager`/OAuth and `youtube_privacy` — meaning this code is
demonstrably still active and likely to be touched again) and Thumbnail
Service (largest, most actively evolving agent cluster per `CLAUDE.md`'s
Video DNA and cinematic-prompt-architecture notes, currently mid-development
per the uncommitted working tree).

### 4. Explicitly not a convergence target right now

- **Discovery, Research, Script, Visual, Voice, Assembly Services** — no
  open defect or planned feature currently touches these. Leave as direct
  agent-module calls until a real reason to touch them arises.
- **Workflow Registry** (`WORKFLOW_MODEL.md`'s declarative branching) —
  still explicitly deferred per the Alignment Report; nothing here changes
  that. One linear Channel Package, one linear workflow, no evidenced need
  for branching or a registry yet.
- **Channel Package structural boundary** and **dashboard business-logic
  audit** (Alignment Report conflicts #3 and #4) — unrelated to the
  Service layer question; not addressed by this plan.

---

## What a Service looks like here, when one is introduced

Kept intentionally minimal, specified now so the first real instance
(Analytics, or an opportunistic extraction) doesn't have to invent the
shape from scratch:

- A Service is a module (or small package) under `hypogeum/services/`,
  named for its domain (`analytics_service.py`, `publishing_service.py`).
- It exposes one stable entry point matching `SERVICE_MODEL.md`'s
  lifecycle in miniature: accepts validated prior-stage output plus
  execution context, returns a validated output package, raises on
  business-rule/validation failure exactly as the agent module already
  does today (`SchemaError` semantics unchanged).
- Internally, it may call one or more existing agent modules — a Service
  does not require rewriting agent modules, only owning the boundary
  around them. `SERVICE_MODEL.md`'s own "Composition" section explicitly
  allows a Service to use Pipelines/Tasks/Workers internally; today's
  agent modules satisfy that role unchanged.
- It does not call another Service directly (`SERVICE_MODEL.md`'s
  Communication rule) — only the Core (`pipeline.py`, or later a real
  Workflow) mediates that.
- No new registration/discovery mechanism is required yet — a plain
  Python import from the relevant `_run_*` wrapper is sufficient until
  there are enough Services that a registry earns its complexity
  (consistent with deferring the Workflow Registry above).

---

## What this plan does not do

- Does not schedule any extraction. No task, ticket, or deadline is
  created by this document.
- Does not touch `hypogeum/pipeline.py`, any `agents/*.py` module, or the
  frozen stage order.
- Does not block or gate Phase 3/Phase 4 work — it specifies how that
  work should be shaped when it starts, not a prerequisite to starting it.
- Does not resolve Alignment Report conflicts #2, #3, or #4 (Workflow
  Registry, Channel boundary, dashboard business logic) — out of scope for
  the Service Layer correction this plan responds to.

## Recorded decision

This plan is recorded as `DECISION_LOG.md` ADR-008: real Service layer,
built incrementally, subordinate to Roadmap sequencing at every step —
never prioritized ahead of Runtime work because a given capability is
architecturally easier to build as a Service.
