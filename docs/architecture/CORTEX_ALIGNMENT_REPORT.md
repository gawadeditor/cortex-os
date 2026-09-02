# Architecture Alignment Report — Implementation vs. the Cortex Constitution

**2026-07-26 phase-numbering note**: `ROADMAP.md` was renumbered after
this report was written. Phase references below use the pre-renumber
scheme (Phase 3 = Analytics, Phase 4 = Learning, Phase 6 = Multi-Channel
Platform). Current numbering: Analytics = Phase 4, Learning = Phase 5,
Multi-Channel Platform = Phase 8. Two new phases were inserted before
Multi-Channel: Phase 3 (Production Hardening, currently active) and
Phase 6 (Factory Capability Evolution, `DECISION_LOG.md` ADR-012).
`ROADMAP.md` is authoritative for phase numbers; this report's analysis
is otherwise unaffected.

Produced 2026-07-25, immediately after migrating the documentation set.
Documentation-only change; no code was modified to produce this report.

## What was done before this report

1. **Read the source files directly** at `/Users/gawadeditor/Desktop/Source
   Files` — all 17 were RTF (TextEdit's native format) saved with a `.md`
   extension, not actual Markdown. Extracted clean text via `textutil`
   before doing anything else, so nothing RTF-encoded ever reached the repo.
2. **Read all 17 documents in full** before forming any judgment.
3. **Archived (not deleted)** 8 documents whose role is now superseded,
   into `docs/archive/pre-cortex-2026-07-25/`, using `git mv` where the
   files were tracked so history is preserved. See that folder's
   `README.md` for the exact old → new mapping.
4. **Installed the 17 canonical Cortex documents at repo root**, matching
   the existing convention (`CLAUDE.md`, and previously `DECISION_LOG.md`
   / `CURRENT_PHASE.md`, already lived at root, not under `docs/`).
5. **Left `CLAUDE.md` untouched.** Nothing in the Cortex set addresses
   concrete implementation detail at CLAUDE.md's level (exact module
   names, the frozen pipeline stage order, DB schema, integration
   contracts like "never call `os.getenv()` directly"). It is not
   superseded — it serves a different, still-necessary purpose.
6. **Left Hypogeum-channel-specific content untouched**
   (`docs/brand_guardian/*`, `hypogeum/skills/visual_package_director/
   cinematic_prompt_architecture.md`, etc.) — per `CHANNEL_PACKAGE_SPEC.md`,
   this content belongs to the Hypogeum *Channel Package*, not to Cortex
   Core, so it isn't part of what this migration touches.
7. **All file moves/adds are currently staged, not committed** — holding
   the commit until this report is reviewed, since it's a wide,
   visible change even though it's documentation-only.

One naming collision required a judgment call: both the old and new sets
have a `DECISION_LOG.md`, but the new one is explicitly architecture-ADRs-
only (its own Governance section: *"Bug fixes do not belong here"*),
while the old one is a real ~15-entry operational/engineering decision
history (MAR, H1/H2, OAuth, this session's actual work). These are
different documents that happen to share a name. The old one is archived
intact, not merged or deleted, and the new canonical `DECISION_LOG.md`
starts empty of ADRs at root. **Open question, not decided here:** where
future operational/bug-fix decisions should continue being logged now
that the canonical `DECISION_LOG.md` cannot hold them by its own rule.

---

## Fully Aligned Areas

- **Channel Package concept**: `ChannelProfile` + `ChannelOperationsProfile`
  + `BrandProfile` + `PublishingProfile` + `ModelProfile`
  (`hypogeum/channel_config.py`) already implement most of
  `CHANNEL_PACKAGE_SPEC.md`'s sections — Identity, Audience (via brand
  bible), Editorial/Visual/Thumbnail DNA, Publishing Strategy, Platform
  Configuration, Secrets (via `secret_profiles`). Objectives/Learning are
  the least-built parts, consistent with `CURRENT_STATE.md`'s own
  "Learning: Early" status.
- **Provider abstraction**: `muapi.py` / `openrouter.py` / `elevenlabs.py`
  / `perplexity.py` implement the `Provider` entity from `DATA_MODEL.md`,
  with tier-based routing already supporting provider replacement
  (`MUAPI_DEFAULT_TIER`, per-tier model overrides) without touching
  calling code — matches `DESIGN_PRINCIPLES.md` Principle 9
  (Replaceability) directly.
- **Skills framework**: `hypogeum/skills/` already exists and matches
  `SKILLS_SPEC.md`'s model (compiled, channel-independent knowledge
  packages — video DNA, visual language engine, translation skills).
- **Fail-closed safety**: directly validated *today*, not retroactively
  inferred — the `youtube_privacy` fix built this session
  (`hypogeum/worker_integration.py`) is a textbook implementation of
  `PROJECT_NORTH_STAR.md`'s Safety Principle ("Cortex must fail closed.
  Never fail open... Examples: Publishing privacy") and
  `DESIGN_PRINCIPLES.md` Principle 13.
- **Single Source of Truth**: also directly validated today — the OAuth
  credential-resolution fix (`secret_profiles` as canonical over stale
  `.env`) is *literally* `FACTORY_PHILOSOPHY.md`'s own worked example
  under Principle 23 ("OAuth → Secret Profiles").
- **Evidence-based decisions, never intuition**: matches the evidence
  discipline used throughout this session's MAR validation and repair
  work (raw DB/log evidence for every claim, hypotheses classified before
  being trusted).
- **Phase 1 exit criteria** ("One complete production cycle executes
  without routine human intervention") and **Phase 2 exit criteria**
  ("Cortex runs continuously") from `ROADMAP.md` — both achieved this
  session: MAR (Minimum Autonomous Runtime) plus today's first fully
  autonomous end-to-end production run with a real private YouTube upload.
- **Configuration over hardcoding**: `Config` as the sole env-var read
  point, `ChannelOperationsProfile` as data-driven policy — matches
  `FACTORY_PHILOSOPHY.md` Principle 5 closely, including its own negative
  example (`if channel == "Hypogeum"`); no such hardcoded branch exists
  anywhere in the pipeline — `channel_id` is always a parameter.

---

## Minor Inconsistencies

- **Terminology**: `GLOSSARY.md` prescribes "Workflow" not "Pipeline",
  "Service" not "Module", "Agent" not "Worker", "Execution" not "Run" —
  the codebase uses `pipeline.py`, `run_worker.py`, a `runs` DB table,
  `hypogeum/agents/`. The Glossary itself permits this ("Implementation
  may use different internal names") — not a required rename, just a
  note that new *architectural documentation* should use the canonical
  terms even where code doesn't.
- **`CURRENT_STATE.md` is already stale** relative to today: it lists
  "Autonomous Runtime: In Progress" with remaining objectives
  (continuous execution, background scheduling, recovery from failures)
  that are now substantially done (MAR built, validated, and repaired;
  H1/H2 fixed; a real autonomous production+publish cycle completed).
  This is expected for a living document written before today's work
  landed — flagged as the first required migration below, not a defect.
- **Unused `channels` DB table**: discovered during MAR validation this
  session — the table exists but is empty; real channel identity lives in
  `channel_profiles` instead. Minor schema hygiene, unrelated to Cortex
  specifically, but worth noting since `Channel` is a canonical entity in
  `DATA_MODEL.md`.
- **Governance hierarchy lists 15 documents; 17 were provided.**
  `DESIGN_PRINCIPLES.md` and `GLOSSARY.md` exist but aren't in
  `GOVERNANCE.md`'s numbered hierarchy. Cosmetic — worth a one-line fix
  in `GOVERNANCE.md` itself at some point, not urgent.

---

## Major Architectural Conflicts

1. **No Service layer exists.** `EXECUTION_MODEL.md` / `SERVICE_MODEL.md`
   / `AGENT_MODEL.md` describe three distinct layers — Core →
   Service → Agent — with Agents belonging to exactly one Service and
   never communicating Agent-to-Agent. The actual pipeline
   (`hypogeum/pipeline.py::run_pipeline()`) calls each
   `hypogeum/agents/*.py` module directly in sequence, with one agent's
   output package passed straight into the next agent's input — there is
   no intermediate Service abstraction mediating that handoff today.

2. **No Workflow Registry; the pipeline is imperative and linear, not
   declarative.** `WORKFLOW_MODEL.md` calls for registered Workflows with
   Stages that can branch and run in parallel. The actual pipeline is a
   single hardcoded Python function with a CLAUDE.md-documented **frozen**
   stage order (EP→RD→NA→SE→Thumbnail Strategy→VPD→TD→Audio→Assembly→
   Publishing) — no registry, no declarative stage definitions, no
   branching or parallel execution.

3. **Cortex Core is supposed to "know nothing about any specific
   channel"; the current repo doesn't structurally enforce that
   boundary.** Generic factory-OS logic (queue, scheduler, pipeline
   orchestration) and Hypogeum-specific content (brand bible, cinematic
   prompt architecture) currently live inside the same `hypogeum/`
   package/repo, not behind a clean Channel Package directory or package
   boundary. `FACTORY_PHILOSOPHY.md` Principle 24 already anticipates
   this exact tension ("Hypogeum validates Cortex. Hypogeum is not
   Cortex.").

4. **Dashboard contains real business logic in places — and the
   pre-existing Dashboard *design* documentation makes this pervasive,
   not incidental.** ADR-004 and `CORTEX_ARCHITECTURE.md` Rule 3 both say
   Interfaces never own business logic. `run_dashboard.py` (5,600+ lines)
   does real work inline in route handlers in at least one place directly
   observed this session — the YouTube/Meta OAuth token-exchange HTTP
   calls live directly inside `youtube_oauth_callback()`/
   `meta_oauth_callback()`, not delegated to a Service. The Minimum
   Operational Dashboard work earlier this session was deliberately
   disciplined about keeping *new* dashboard code read-only/thin — this
   conflict predates that work and wasn't in scope for it.

   **Reconciliation pass (2026-07-26)** against
   `docs/dashboard-product-architecture.md` and
   `docs/dashboard-architecture-system.md` (the pre-Cortex-Constitution
   B5.x Dashboard design specs) found this is not a one-off — it is how
   the "Dashboard Backend API" concept was designed throughout. Concrete
   examples directly from those documents: "Dashboard Safety Rules" (§8
   of `dashboard-product-architecture.md`) describes the API itself
   *rejecting* run requests for inactive channels, *rejecting*
   `update_channel()` calls without an approved `ModelSwapRequest`,
   *rejecting* module operations with `ModuleInUseError`, and applying
   `channel_id IN [assigned_channels]` RBAC filtering directly — all real
   business rules, described as owned by the Dashboard's own backend, not
   a Service it calls. Global Search (§9.1 of `dashboard-architecture-system.md`)
   is described as an endpoint that "queries entity managers" directly.
   As written, implementing these legacy specs literally would build an
   entire Interface-layer backend that owns exactly the business logic
   Cortex's architecture forbids it from owning.

   This is a decision to make before real Dashboard implementation
   resumes (see `SERVICE_LAYER_MIGRATION_PLAN.md` and ADR-008 for the
   related Service-layer convergence context) — not something to silently
   carry forward into new Dashboard work. Not fixed or rewritten here;
   flagged as a Major Conflict requiring an explicit decision, per
   `GOVERNANCE.md`'s "documentation precedes implementation" rule.

---

## Required Migrations (recommendations only — nothing implemented)

In suggested order:

1. **Update `CURRENT_STATE.md`** to reflect what's actually true as of
   today (MAR complete, H1/H2 fixed, OAuth and youtube_privacy defects
   fixed, first autonomous production run achieved). Zero code risk,
   immediate value, and a prerequisite for grading anything else against
   an accurate baseline. This is explicitly what the document exists for.
2. **Decide, explicitly, how the missing Service layer is being
   interpreted** — either (a) treat `pipeline.py::run_pipeline()` itself
   as the one registered "Production Workflow" and each agent module as
   directly *being* the Agent (accepting that the Service layer is
   currently collapsed into the Core, a smaller gap than it first
   appears), or (b) plan a real Service-layer extraction. This is a
   decision to record (an ADR), not a code change to start yet — everything
   downstream depends on which reading is intended.
3. **Continue `ROADMAP.md`'s own next phases** (Phase 3: Analytics,
   Phase 4: Learning Engine) — Phases 0–2 are now substantially satisfied
   by this session's work, and `CURRENT_STATE.md`'s own "Current
   Priorities" #1–#2 (reliable autonomous runtime, unattended end-to-end
   production) are both achieved.
4. **Only after that**, consider the larger structural items: explicit
   Service-layer extraction (if #2 above chooses that path), a real
   Channel Package directory/package boundary, and auditing
   `run_dashboard.py` for business logic to relocate. These map to
   `ROADMAP.md` Phase 6 (Multi-Channel Platform) territory, which is
   correctly *not* next per the roadmap's own "Core before Interface,
   Platform before Product" ordering principle.
5. **Explicitly defer** a Workflow Registry / declarative branching
   engine — no current evidence of need (one Channel Package, one linear
   production workflow, working) — revisit only when a second real
   Channel Package or genuine parallel-stage requirement appears.
6. **Resolve the open question above**: where ongoing operational/
   bug-fix decisions get recorded now that canonical `DECISION_LOG.md` is
   architecture-ADRs-only.

---

## What This Report Does Not Do

No code was modified. No architectural refactoring was performed. The
git working tree currently has the documentation moves/adds **staged but
not committed**, pending your review of this report.
