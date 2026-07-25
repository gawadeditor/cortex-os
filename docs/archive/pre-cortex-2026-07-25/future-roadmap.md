# The Hypogeum Content Factory — Implementation Roadmap

**Last updated:** 2026-07-19
**Status:** Canonical, authoritative implementation sequence for Hypogeum Brain. See "Roadmap Governance" below for how this document relates to the rest of the governance layer.

This revision is a substantial correction, not a cosmetic edit. A real audit of the codebase (pipeline stage list, module wiring, git history) found that several items long-marked "Deferred" in earlier versions of this document are actually built and active in production. See "Corrections Made in This Revision" near the end for the full list, with evidence.

---

## Roadmap Governance

This roadmap is one document in a governance hierarchy. Authority order, most to least foundational:

1. **`PROJECT_NORTH_STAR.md`** — long-term vision (rarely changes)
2. **This document (`docs/future-roadmap.md`)** — canonical, sequenced implementation path toward that vision
3. **`CURRENT_PROGRESS.md`** — live, detailed status snapshot (what's done/active/blocked *today*)
4. **`EXECUTION_PROTOCOL.md`** — execution rules for every implementation task
5. **`DECISION_LOG.md`** and **`CURRENT_PHASE.md`** — architectural decisions and current-phase context

`PROJECT_CONSTITUTION.md` and `PROJECT_PRINCIPLES.md` remain independently authoritative for current-phase decision rules and operating principles respectively — this roadmap must not contradict either.

**Update discipline:** this document is updated whenever a workstream completes, freezes, or is added — not on a fixed schedule. Completed items are marked complete and kept as historical record, never deleted. Sequencing changes require the same evidence standard as any other change under `EXECUTION_PROTOCOL.md` §2 (investigation before action) — do not resequence based on assumption. Governance is lightweight: these updates exist to support implementation, not replace it. Once the governance documents are internally consistent, implementation takes priority — a governance update normally accompanies a completed workstream and should not itself become a standalone workstream unless the governance layer is actually broken (contradictory, stale, or blocking real decisions).

---

## Phase Sequence

Mirrors `CURRENT_PHASE.md`'s phase chain exactly. This roadmap does not introduce a competing sequence — it is the detailed implementation breakdown *within* each phase.

| # | Phase | Status |
|---|---|---|
| 1 | Architecture Stabilization | **ACTIVE** — Governance & Roadmap Synchronization sub-workstream complete 2026-07-19 |
| 2 | Dashboard Platform | **ACTIVE / NEXT** — promoted ahead of Full Manual Episode Validation, 2026-07-19 (see Sequencing Rationale below) |
| 3 | Full Manual Episode Validation | Blocked (YouTube OAuth) — mandatory before declaring production readiness, no longer gates other engineering work |
| 4 | Runtime | Not started |
| 5 | Learning Loop | Not started |
| 6 | Model Evaluation Framework | Not started |
| 7 | SaaS | Not started |

**Sequencing Rationale — Dashboard Platform promoted ahead of Full Manual Episode Validation (2026-07-19):**

- Full Manual Episode Validation is blocked by an external infrastructure dependency (YouTube OAuth token re-authorization) that engineering work cannot resolve.
- Dashboard Platform development has no dependency on that OAuth token being operational.
- Therefore Dashboard Platform is the highest-value engineering workstream available while the OAuth issue is pending — continuing to gate it behind an unrelated external blocker would waste available engineering time.
- Full Manual Episode Validation remains mandatory before the project can be declared production-ready. It is not removed from the sequence, only reordered — it no longer blocks unrelated engineering work.

This decision explicitly supersedes the reasoning in `DECISION_LOG.md`'s prior entry ("Dashboard UX begins after production pipeline is validated... Avoid polishing UI before backend stability"). That entry was **not edited or removed** — decision logs are append-only historical record — a new entry recording this supersession, with the rationale above, was appended to `DECISION_LOG.md` 2026-07-19.

---

## Phase 1 — Architecture Stabilization [CURRENT / ACTIVE]

### Completed & Frozen (historical record)

Frozen = do not reopen without a specific new production issue requiring it. Full detail on each item lives in `CURRENT_PROGRESS.md`; this is the roadmap-level record of what shipped.

| Item | Frozen | Was |
|---|---|---|
| M0–M5.5 — core pipeline (EP→RD→NA→SE→VPD→TD→Audio→Assembly→Publishing) | — | Original 9-agent pipeline |
| Humanizer | — | Script Engine skill (M5.4B) |
| Prompt Compiler (Stage 0–5, Provider Adapters) | 2026-07 | New this phase |
| Video DNA v1 architecture | 2026-07-14 | New this phase — per-concept modules (camera/motion/lighting/historical/composition/forbidden DNA) |
| Graphify integration | active | New this phase — repository knowledge graph, in active use |
| Seedance translation skill | active (partial) | New this phase — `hypogeum/skills/seedance_translation_skill.md`; generalizes camera/motion vocabulary per-provider, not yet a full replacement for hand-tuned VPD prompts |
| **M9.1 — QA Agent** | active | `hypogeum/agents/qa_agent.py`, wired as the real `qa_agent` pipeline stage |
| **M9.2 — Compliance Agent** | active | `hypogeum/agents/compliance_agent.py`, wired as the real `compliance_agent` pipeline stage |
| **M7.1 — Finance Controller (core)** | active | `hypogeum/finance.py` + `hypogeum/agents/finance_agent.py`, wired as `finance_agent_authorization` (pre-VPD) and `finance_agent_ledger` (post-run) stages. Cost *tracking* only — reconciliation against real provider billing is separate, see Platform Observability below |
| **M7.2 — Asset Archive Agent** | active | `hypogeum/agents/asset_archive_agent.py`, wired as the real `asset_archive` pipeline stage |
| **M8.1 — Knowledge Graph (seed extraction)** | active | `hypogeum/knowledge_graph.py`, wired via `post_run_intelligence.py` as a non-blocking post-run flow |
| **M8.3 — World Bible** | active | `hypogeum/world_bible.py`, wired via `post_run_intelligence.py` as a non-blocking post-run flow |
| **M10.1 — Brand Guardian Agent / Thumbnail DNA v1–v2** | 2026-07-19 | Confirmed against a real MUAPI provider call (no more prompt-rejection failures) |
| **M10.2 — `docs/channel-identity.md`** | complete since 2026-06-12 | v1.0, partial enforcement coverage documented in the file itself |
| Thumbnail Strategy pipeline wiring | 2026-07-19 | Was previously reachable only via a standalone script |
| Production safety gate (`production_gate.py`) | 2026-07-13 | Full production blocked by default |
| Music Architecture Option B | 2026-07-19 | Section/category-based generation restored per the frozen ElevenLabs spec |
| Thumbnail composition-distinctness fix | 2026-07-19 | Candidate-assembly-layer resolver; `_TRIGGER_MAP` untouched |
| Budget-ceiling clearance gate + `validation_mode` exemption | 2026-07-18 | |
| MUAPI dry-run placeholder fix (video + thumbnail) | 2026-07-18/19 | |

### Remaining in this phase

1. **Platform Observability & Financial Intelligence** — provider reconciliation + cost ledger audit. Recorded 2026-07-19, not started. Full spec below under "Cross-Cutting."
2. **M7.3 — Reuse Recommendation Engine wiring** — `hypogeum/reuse_engine.py` is built and tested but has **zero callers anywhere in the codebase**. Not integrated into VPD Phase 1/Phase 2. Full spec preserved under Phase 4 (Runtime) below, since reuse value only materializes at episode volume — but the wiring gap itself is an architecture-stabilization item.
3. **Log isolation / rotation** — `pipeline.log` is ~24GB, unrotated, polluted by test writes. Not a hard blocker but materially slows debugging (confirmed: a `grep -c` against it timed out during the 2026-07-19 validation campaign).
4. **Dedicated validation-run authorization mode** — `production_gate.py` only recognizes full-production authorization; every supervised validation campaign currently requires a temporary full-production override, manually reverted afterward. A narrower `PRODUCTION_MODE=supervised_validation` state would remove this friction.

---

## Phase 2 — Dashboard Platform [ACTIVE / NEXT]

Promoted ahead of Full Manual Episode Validation 2026-07-19 — see Sequencing Rationale above. Not yet started as of this update (governance documentation was the prerequisite; implementation begins after this document is approved). Detailed scope will be established when implementation formally begins, per `EXECUTION_PROTOCOL.md` (investigate before implementing; no roadmap content should be speculatively written before a phase's own investigation happens).

Note: `.claude/skills/dashboard_ux.md` already exists as a prepared skill file, suggesting some prior prep work exists for this phase even though implementation has not begun.

---

## Phase 3 — Full Manual Episode Validation [BLOCKED — mandatory before production readiness]

**Definition of done:** one complete, real (non-`MUAPI_DRY_RUN`) episode, produced and published through the real production route end-to-end, with no manual patching of state to force success.

**Status as of 2026-07-19:**
- Two supervised validation campaigns completed with `MUAPI_DRY_RUN=true` (video/thumbnail-placeholder mode) — these validated pipeline correctness, the music architecture, and (as of Run 2) surfaced and fixed the thumbnail composition-distinctness bug.
- **Blocking:** YouTube OAuth token refresh is currently failing (HTTP 400) — no real publish can succeed until re-authorized. This is an operator/infra action, not a code fix.
- A real, non-dry-run, full-spend episode has not yet been run end-to-end since this blocker appeared.
- **This phase no longer blocks Phase 2 (Dashboard Platform) or any other engineering work** — see Sequencing Rationale above. It remains mandatory before the project is declared production-ready.

---

## Phase 4 — Runtime

**Goal:** Separate development, production, and archive environments. Enable unattended, multi-episode operation.

### M7.3 — Reuse Recommendation Engine (wiring)

**Purpose:** Reduce per-episode generation cost and increase visual consistency by recommending existing archived assets before generating new ones.

**Current state:** `hypogeum/reuse_engine.py` exists, is unit-tested (`tests/test_reuse_engine.py`), but is not called from anywhere in the pipeline. The Asset Archive it would search (M7.2) is already active and accumulating assets, so the data this engine needs exists — only the integration is missing.

**Responsibilities:** Search Asset Archive for candidates matching current scene brief; score candidates by relevance (entity/period/style match); estimate reuse value (cost avoided vs. quality delta); output ranked reuse candidates; recommend generate-vs-reuse per scene.

**Integration point:** Between VPD Phase 1 (planning) and Phase 2 (generation).

**Status:** Deferred — meaningful once episode volume exists to reuse against.

### M11 — Infrastructure & Operations

**Current Hardware Assets**

- **MacBook Pro M4 Max** — primary workstation, current development and production machine.
- **Intel Mac mini** — existing asset, older/slower, capable of 24/7 unattended operation. Candidate roles: orchestration, scheduling, publishing, archive node.
- **Future dedicated production machine** — evaluate only after production throughput demonstrates the need. Do not acquire speculatively.

**Long-Term Infrastructure Requirements**

- Environment separation: development (M4 Max) / production (future dedicated machine or Intel mini) / archive (separate, redundant storage).
- Backup strategy: local + offsite for all generated assets and code.
- Storage strategy: tiered (hot for recent episodes, cold for archive); retention policy: all generated assets retained indefinitely.
- Monitoring: provider uptime, cost alerts, job failure alerts.
- Disaster recovery: credential backup, asset recovery procedure.
- Credential management: rotation schedule, secure storage, no hardcoded keys.
- Provider outage handling: graceful degradation, fallback model selection.

**Status:** Deferred. Evaluate after production is validated at volume.

---

## Phase 5 — Learning Loop

**Goal:** Give the factory long-term memory of everything it has produced, and awareness of how that output performs, so future output improves without manual intervention.

### M8.2 — Continuity Agent

**Purpose:** Prevent contradictions, repeated narratives, and terminology inconsistencies across episodes. Distinct from `entity_continuity_manager.py` (active today, but scoped to *within-episode* visual entity consistency, not *cross-episode* contradiction detection).

**Validates:** conflicting historical claims (dates, attributions, measurements); repeated narratives (near-duplicate episodes/beats); timeline conflicts across episodes; inconsistent terminology; recurring-fact consistency over time.

**Outputs:** `continuity_warnings_{episode_id}.json`, `canon_validation_report_{episode_id}.json`.

**Integration point:** After Script Engine, before Visual Package Director. Blocking issues halt production.

**Status:** Deferred — meaningful at 5+ episodes; requires M8.1 (active) as its data source.

### M8.4 — Portfolio Manager Agent

**Purpose:** Manage long-term editorial diversification. Prevent overproduction of any one content category.

**Tracks:** topic mix by category; series mix (standalone vs. multi-part); allocation by historical period; geographic distribution; audience saturation signals.

**Feeds into:** Executive Producer topic scoring.

**Status:** Deferred — meaningful at 20+ episodes; requires M8.1 (active) as its data source.

### M6.1 — Performance Intelligence Agent

**Purpose:** Analyze real platform performance data and generate structured optimization signals back into upstream agents.

**Inputs:** YouTube/TikTok/Instagram/Facebook Analytics (CTR, retention, impressions, hook completion, average view duration, end-screen clicks).

**Responsibilities:** CTR analysis per thumbnail/title/topic; retention and hook analysis; thumbnail A/B results; topic over/under-performance vs. baseline; audience-segment analysis; best publishing windows; Shorts vs. long-form ROI.

**Outputs:** `performance_report_{episode_id}.json`, `channel_performance_summary.json`, `optimization_signals.json`.

**Feeds into:** Executive Producer, Research Director, Narrative Architect, Script Engine, Thumbnail Director, Publishing Agent.

**Status:** Deferred — trigger: 20–50 published videos with ≥7-day post-publish analytics window. None published for real yet (blocked by Phase 2).

### M6.2 — Audience Memory Agent

**Purpose:** Maintain long-term memory of audience behavior, accumulated across all runs — distinct from M6.1, which analyzes each run individually.

**Stores:** successful/failed hooks, topics, thumbnails; successful publishing windows; audience preferences/aversions; long-term engagement patterns.

**Feeds into:** Executive Producer, Script Engine, Thumbnail Director.

**Status:** Deferred — requires M6.1.

---

## Phase 6 — Model Evaluation Framework

Per `DECISION_LOG.md` ("Model evaluation framework becomes part of the factory... the system must continuously discover and upgrade the best models automatically") and `PROJECT_CONSTITUTION.md`'s priority list. Not yet specified in detail — specification is Phase 6's own first task when this phase becomes active, per `EXECUTION_PROTOCOL.md` (no speculative detail for unstarted phases).

**Status:** Not started.

---

## Phase 7 — SaaS

Distant. Multi-tenant platform productization of the factory, per `PROJECT_NORTH_STAR.md`'s "new channels are created by cloning the template" vision. No detailed specification exists yet.

**Status:** Not started.

---

## Cross-Cutting: Platform Observability & Financial Intelligence

**Recorded:** 2026-07-19, during the Music Architecture Option B investigation, which surfaced a real cost-ledger bug — `generate_music_track()` recorded the pre-clamp *requested* duration instead of the actual clamped/generated duration in cost, library-metadata, and returned-result fields. The bug went undetected because internal cost figures were never reconciled against actual ElevenLabs billing. This initiative generalizes that finding into a standing platform requirement.

**Relationship to M7.1 Finance Controller:** M7.1 (active, Phase 1) covers cost *tracking*. This initiative is broader: *reconciliation* (verifying tracked figures match reality), spanning every provider integration and the Dashboard's cost display. Not a replacement for M7.1 — deepens its guarantees.

**Scope:**

1. **Provider Reconciliation** — for every provider (OpenRouter, ElevenLabs narration, ElevenLabs music, MUAPI, future providers), verify internally recorded cost/usage matches the provider's own billing. Repeatable check per provider, not a one-off audit.
2. **Cost Ledger Audit** — review the `costs` table and every `finance.record_cost()` call site for: actual vs. estimated usage; retry waste; cache hit/miss cost accuracy; duplicate-recording prevention.
3. **Dashboard Accuracy** — dashboard cost totals reconciled against both the internal ledger and real provider billing; estimated/provisional/confirmed costs visually and structurally distinguished.
4. **Financial Intelligence** — cost per factory/channel/episode/scene/minute/agent/provider/model, plus forecasting, burn-rate tracking, budget alerts.

**Permanent rule (binding on all future cost and architecture decisions):**

> No provider, model, or architectural cost decision may rely on internal cost figures until those figures are reconciled against the provider's actual usage or billing data.

Applies retroactively to prior cost claims in this codebase's history and prospectively to all future cost-driven decisions.

**Status:** Recorded, not started. Belongs in Phase 1 (Architecture Stabilization) — see "Remaining in this phase" above.

---

## Mandatory Publishing Requirements

Every published episode must include all four of the following. Permanent platform requirements, not features to add later:

1. **Source attribution section** — inline in YouTube description
2. **Chapter markers** — timestamps in YouTube description
3. **Uploaded SRT captions** — via YouTube API, not auto-generated
4. **AI disclosure section** — in YouTube description and (where required) on-screen

---

## Deferred Skills

### Seedance2 (full VPD generalization)
**Status:** Partially addressed by the active Seedance translation skill (Phase 1) and Video DNA v1's camera/motion DNA modules. Full evaluation of camera/shot/motion vocabulary as fully generic, provider-agnostic VPD fields remains open. Do not import Seedance-specific syntax into the VPD prompt or schema.

### Journalism Skill
**Status:** Deferred, unchanged. Extract SIFT methodology, source hierarchy, evidence-strength model, and verification framework as Research Director policy — via prompt and schema, not a runtime slash-command skill.

---

## Corrections Made in This Revision (2026-07-19)

Recorded for audit trail — this document was previously significantly stale. Each correction below was verified against real code/wiring, not assumed:

| Item | Was | Now | Evidence |
|---|---|---|---|
| M9.1 QA Agent | "Deferred" | Active | `hypogeum/agents/qa_agent.py`, real `qa_agent` pipeline stage, confirmed executing in 2026-07-19 validation runs |
| M9.2 Compliance Agent | "Deferred" | Active | `hypogeum/agents/compliance_agent.py`, real `compliance_agent` pipeline stage |
| M7.1 Finance Controller | "Deferred" | Active (core) | `finance.py` + `finance_agent.py`, real `finance_agent_authorization`/`finance_agent_ledger` stages |
| M7.2 Asset Archive Agent | "Deferred" | Active | `asset_archive_agent.py`, real `asset_archive` pipeline stage |
| M7.3 Reuse Engine | "Deferred" | Built, not wired | `reuse_engine.py` exists and is tested; zero callers found anywhere in the codebase |
| M8.1 Knowledge Graph | "Deferred" | Active (seed extraction) | `knowledge_graph.py` via `post_run_intelligence.py`, non-blocking post-run flow |
| M8.3 World Bible | "Deferred" | Active | `world_bible.py` via `post_run_intelligence.py`, non-blocking post-run flow |
| M10.1 Brand Guardian / Thumbnail DNA | "Deferred" | Frozen/active | Confirmed against real MUAPI provider call, 2026-07-19 |
| M10.2 `channel-identity.md` | "Document not yet created" | Complete | File exists, v1.0, since 2026-06-12 |
| Graphify | "Deferred... evaluate only if... a bottleneck" | Active | `graphify-out/` contains real analysis data; in active use this session |
| Seedance2 Skill | "Deferred" | Partial | `seedance_translation_skill.md`/`.compact.md` exist and are compiled |
| Music Architecture (Option B) | Not present in this document at all | Frozen, 2026-07-19 | Entire workstream this document never tracked |
| Prompt Compiler / Video DNA v1 | Not present in this document at all | Frozen/active | Entire workstream this document never tracked |

No item was marked complete without direct evidence (file existence + real pipeline wiring, or a specific dated freeze record). Where evidence was ambiguous (M8.2 Continuity Agent vs. `entity_continuity_manager.py`), the item was kept as deferred and the distinction was noted explicitly rather than guessed.

---

*This document is the single authoritative implementation roadmap for The Hypogeum Content Factory. Do not implement any item listed here without an explicit session directive. Do not resequence phases without updating `CURRENT_PHASE.md` to match.*
