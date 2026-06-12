# Octogent Agent & Skill Audit

**Date:** 2026-06-06  
**Status:** Audit only. No implementation.  
**Purpose:** Reconcile the agreed future roadmap with the actual repository state. Determine what skills are missing, where they belong, and how to add them correctly.

---

## 1. Current Octogent Agents / Workstreams

The project uses four Octogent tentacles, each grouping related agents:

| Tentacle | Agents | Status |
|---|---|---|
| `editorial-pipeline` | Executive Producer, Research Director, Narrative Architect, Script Engine | Complete |
| `visual-production` | Visual Package Director, Thumbnail Director | Complete |
| `provider-integrations` | Provider architecture (source of truth — not agents) | Complete |
| `distribution` | Audio Agent, Assembly Agent, Publishing Agent | Complete |

**Tentacle definitions live at:** `.octogent/tentacles/<name>/CONTEXT.md`

Each tentacle also has:
- `memory/` — persistent tentacle-scoped state files
- `todo.md` — implementation tracking

**Total production agents implemented:** 9  
**Total production agent Python files:** 9 (in `hypogeum/agents/`)  
**Total system prompt files:** 6 (in `hypogeum/prompts/`)

---

## 2. Current Vault Files

### Core Documentation (`docs/`, 25+ files)

| Category | Files |
|---|---|
| Strategic | `master-project-brief.md`, `roadmap.md`, `future-roadmap.md`, `frozen-components.md` |
| Implementation | `implementation-roadmap-phase1.md`, `m5-implementation-plan.md`, `production-readiness.md`, `production-onboarding-plan.md`, `first-production-run.md`, `episode-zero-status.md` |
| Session checkpoints | `session-checkpoint-2026-05-31.md` through `session-checkpoint-2026-06-04.md` |
| Architecture verification | `architecture-index.md`, `final-architecture-audit.md`, `elevenlabs-music-api-verification.md`, `provider-decisions.md`, `shorts_extraction_architecture.md` |
| Brand Guardian (deferred) | `brand_guardian/brand_bible_hypogeum.md`, `brand_guardian/brand_guardian_agent.md`, `brand_guardian/brand_spec_executable.md`, `brand_guardian/extraction_audit.md`, `brand_guardian/gap_report.md`, `brand_guardian/brand_bible_schema.json` |

### Tentacle Context Files (`.octogent/tentacles/`)

20 files across 4 tentacles, including `CONTEXT.md`, `todo.md`, and `memory/` contents per tentacle.

### Memory Files

**Claude Code memory** (`~/.claude/projects/.../memory/`):
- `MEMORY.md` — index
- `project_state.md`, `project_brand_guardian.md`, `project_runway_i2v_fix.md`, `project_seedance_benchmark.md`, `project_voice.md`, `roadmap.md`

**Local project memory** (`memory/`):
- `project_state.md`, `covered-topics.md`

---

## 3. Current Skills Count

**Skills count: 0**

Confirmed by filesystem search:

```
find . -iname "skill" → empty
find . -type d -name "*skill*" → empty
find . -name ".skill" → empty
find . -name "*.md" -path "*skill*" → empty
```

There is no `.claude/skills/` directory. There is no `skills/` directory anywhere in the repository. No skill definition files exist.

---

## 4. Expected Skills from Roadmap

The `docs/future-roadmap.md` (authoritative, last updated 2026-06-05) identifies the following deferred items under "Deferred Skill Roadmap":

| Item | Roadmap Verdict |
|---|---|
| Humanizer | Installed and active as of M5.4B. No further work planned. |
| Seedance2 Skill | Deferred. Camera/shot/motion vocabulary as provider-agnostic VPD fields only. Explicitly prohibited as slash-command. |
| Journalism Skill | Deferred. SIFT + evidence model as RD prompt policy, not slash-commands. |
| Graphify | Deferred. Evaluate only if search/context overhead is a demonstrated bottleneck. |

The roadmap also specifies **post-M5 agents** (M6–M11) that are distinct from Claude Code skills — they are full pipeline agents:

| Milestone | Agent/Component |
|---|---|
| M6.1 | Performance Intelligence Agent |
| M6.2 | Audience Memory Agent |
| M7.1 | Finance Controller Agent |
| M7.2 | Asset Archive Agent |
| M7.3 | Reuse Recommendation Engine |
| M8.1 | Knowledge Graph Agent |
| M8.2 | Continuity Agent |
| M8.3 | Character & World Bible Agent |
| M8.4 | Portfolio Manager Agent |
| M9.1 | Quality Assurance Agent |
| M9.2 | Rights & Compliance Agent |
| M10.1 | Brand Guardian Agent |
| M10.2 | `docs/channel-identity.md` (document, not code) |

---

## 5. Missing Skills — Gap Analysis

The user identified 10 expected skills. This section reconciles each against the actual repository state.

| Expected Skill | In Repo? | What Exists | Gap |
|---|---|---|---|
| Humanizer | **Yes** | `hypogeum/humanizer.py` (active), wired into SE + metadata | No gap. Nothing to add. |
| Seedance2 Prompt | **No** | VPD system prompt has scene parameters but no Seedance-specific vocabulary | Roadmap explicitly defers and prohibits as slash-command. Correct form is VPD prompt policy. |
| Journalism / SIFT | **No** | RD system prompt has general research guidance but no SIFT methodology | Roadmap explicitly defers. Correct form is RD prompt policy, not slash-command. |
| Brand Guardian | **Partially** | `docs/brand_guardian/` has 6 spec documents but no agent code | M10.1 is a full pipeline agent — not a skill. No Python implementation exists. |
| Cost Guardrails / Finance | **Partially** | M5.6 cost guardrails implemented (budget guards, dry-run, cheap mode) | M7.1 Finance Controller (full accounting, ROI, per-episode spend) is not implemented. M5.6 ≠ M7.1. |
| Asset Archive / Reuse | **No** | `.hypogeum-assets/` directory exists but no archive agent or metadata index | M7.2 + M7.3 do not exist as code. |
| Quality Assurance | **No** | No QA agent, no `qa_report_*.json` schema, no publish approval gate | M9.1 does not exist. |
| Rights & Compliance | **No** | Mandatory publishing requirements enforced only via Publishing Agent metadata | M9.2 does not exist. |
| Provider Diagnostics | **Yes** | `scripts/runway_diagnostic.py`, `scripts/seedance_diagnostic.py`, `scripts/validate_credentials.py` | Scripts exist. Not wrapped as a skill. Invocation is manual. |
| Graphify / Repo Navigation | **No** | No knowledge graph, no entity index | Roadmap defers until bottleneck is demonstrated. |

---

## 6. Recommended Agent → Skill Mapping

There are three distinct artifact types in this project. Skills are only one of them. The table below assigns each expected item to its correct artifact type.

### Artifact Type Definitions

| Type | Description | Location |
|---|---|---|
| **Claude Code Skill** | Invokable via `/skillname` at the prompt. A markdown file containing a prompt template or instruction. Executed by Claude Code, not the pipeline. | `.claude/skills/*.md` |
| **Octogent Agent** | A full pipeline agent with Python implementation, Pydantic schemas, system prompt, and tentacle context. Part of the automated production flow. | `hypogeum/agents/*.py` + `.octogent/tentacles/*/CONTEXT.md` |
| **Agent Prompt Policy** | An instruction baked into an existing agent's system prompt (`hypogeum/prompts/*.txt`). Not a separate runtime artifact. | `hypogeum/prompts/*.txt` |
| **Vault Document** | A reference document that informs agent behavior or operator decisions. No runtime execution. | `docs/*.md` |
| **Python Script** | A standalone diagnostic or utility script, manually invoked. Not part of the automated pipeline. | `scripts/*.py` |

### Mapping

| Expected Skill | Correct Artifact Type | Rationale |
|---|---|---|
| **Humanizer** | Python module (done) | Integrated as `hypogeum/humanizer.py`. Roadmap says: no further work planned. |
| **Seedance2 Prompt** | Agent Prompt Policy (in VPD prompt) | Roadmap explicitly says: not a slash-command. Provider-agnostic VPD fields only. No separate skill file. |
| **Journalism / SIFT** | Agent Prompt Policy (in RD prompt) | Roadmap explicitly says: not a slash-command. RD prompt policy only. No separate skill file. |
| **Brand Guardian** | Octogent Agent (M10.1) | Full pipeline agent with validation gate. Spec documents exist but no implementation. |
| **Cost Guardrails** | Octogent Agent (M7.1 Finance Controller) | M5.6 guardrails are input-side controls. M7.1 is a full accounting agent. Different scope. |
| **Asset Archive / Reuse** | Octogent Agent (M7.2 + M7.3) | Full agents with metadata schema, archive index, and reuse scoring. |
| **Quality Assurance** | Octogent Agent (M9.1) | Pipeline gate between Assembly and Publishing. Cannot be a slash-command — it must block publishing. |
| **Rights & Compliance** | Octogent Agent (M9.2) | Pipeline gate. Produces `compliance_report_*.json` and `publishing_approval_*.json`. |
| **Provider Diagnostics** | **Claude Code Skill** (wrapping existing scripts) | Scripts exist. A skill file would let operators invoke `/diagnostics` to run pre-flight validation without remembering script paths. Correct home: `.claude/skills/diagnostics.md`. |
| **Graphify / Repo Navigation** | Deferred (evaluate later) | Roadmap: evaluate only if demonstrated bottleneck. No artifact warranted now. |

### Summary: What Is Actually a Skill?

Of the 10 expected items, only **one** is correctly a Claude Code skill:

- **Provider Diagnostics** → `.claude/skills/diagnostics.md`

All others are either already implemented, correctly belong to the agent/prompt layer, or are future Octogent pipeline agents.

---

## 7. File and Folder Convention Octogent Expects for Skills

### Claude Code Skills

Claude Code skills live in:

```
.claude/skills/<skill-name>.md
```

Each file is a markdown document containing a prompt or instruction template. It is invokable via `/skill-name` at the Claude Code prompt.

**The project currently has no `.claude/skills/` directory.**

Structure for a skill file:

```markdown
# Skill Name

Brief description of what this skill does.

## Usage

/skill-name [optional arguments]

## Prompt

[The prompt template or instruction body that Claude Code executes.]
```

### Octogent Agents (Not Skills)

Pipeline agents are NOT stored as skill files. They require:

1. **Python implementation:** `hypogeum/agents/<agent_name>.py`
2. **Pydantic schemas:** In `hypogeum/schemas.py` (output contracts)
3. **System prompt:** `hypogeum/prompts/<agent_name>.txt`
4. **Tentacle context:** `.octogent/tentacles/<tentacle>/CONTEXT.md` (updated to include the agent spec)
5. **Unit tests:** `tests/test_<agent_name>.py`
6. **Pipeline wiring:** Registered in `hypogeum/pipeline.py`

A skill file cannot substitute for any of these. Implementing a future M7–M10 agent as a skill file would be architecturally incorrect.

---

## 8. Implementation Plan to Add Skills Correctly

### Phase A — Immediate (No implementation blocked)

**A1. Create the skills directory and the one warranted skill.**

```
.claude/skills/diagnostics.md
```

This skill wraps the three existing diagnostic scripts (`validate_credentials.py`, `runway_diagnostic.py`, `seedance_diagnostic.py`) into a single invocable `/diagnostics` command. Operators can run pre-flight validation without remembering script syntax.

No new Python code required. The scripts already exist.

---

**A2. Embed deferred skill content into existing agent prompts.**

The Journalism/SIFT methodology and Seedance2 cinematographic vocabulary were designated as agent prompt policies — not skills. This work is deferred per the roadmap. When the time comes:

- Journalism/SIFT → append to `hypogeum/prompts/research_director.txt`
- Seedance2 vocabulary → append to `hypogeum/prompts/visual_package_director.txt`

Do not create skill files for these.

---

### Phase B — Post-Episode Zero, Pre-Scale

Implement in the sequence prescribed by `docs/future-roadmap.md`:

| Step | Artifact | Correct Type |
|---|---|---|
| B1 | `docs/channel-identity.md` | Vault Document (write, not code) |
| B2 | Quality Assurance Agent (M9.1) | Octogent Agent |
| B3 | Rights & Compliance Agent (M9.2) | Octogent Agent |
| B4 | Brand Guardian Agent (M10.1) | Octogent Agent (requires B1) |
| B5 | Finance Controller Agent (M7.1) | Octogent Agent |
| B6 | Asset Archive Agent (M7.2) | Octogent Agent |
| B7 | Reuse Recommendation Engine (M7.3) | Octogent Agent (requires B6) |

Each of B2–B7 requires a full implementation:  
Python file → schemas → system prompt → tentacle context update → unit tests → pipeline wiring.

**None of these should be implemented as Claude Code skill files.**

---

### Phase C — Deferred (No timeline)

| Item | Trigger |
|---|---|
| Knowledge Graph Agent (M8.1) | 5–10+ produced episodes |
| Continuity Agent (M8.2) | Requires M8.1 |
| Character & World Bible Agent (M8.3) | 5+ produced episodes |
| Portfolio Manager (M8.4) | Requires M8.1 |
| Performance Intelligence Agent (M6.1) | 20–50 published videos |
| Audience Memory Agent (M6.2) | Requires M6.1 |
| Graphify | Demonstrated search/context bottleneck |
| M11 Infrastructure | Production volume warrants it |

---

## 9. Root Cause: Why Skills Were Never Materialized

The agreed skills were never materialized because **most of them were never meant to be skill files**.

The original conversation surfaced a category confusion: the term "skill" was used to describe a range of system capabilities — some of which are Python agents, some are prompt policies, some are documentation, and only one is correctly a Claude Code `/command` skill.

**Specific findings:**

1. **Humanizer** — Implemented correctly as Python code. Not a skill. Never needed to be.

2. **Seedance2 + Journalism** — The roadmap explicitly ruled out slash-command skills for both. They are future agent prompt policies. No skill file should be created.

3. **Brand Guardian, QA, Compliance, Finance, Archive, Reuse** — These are full Octogent pipeline agents. They require Python, schemas, prompts, tests, and pipeline wiring. None of this work can be done in a skill file.

4. **Provider Diagnostics** — The scripts already exist. The only gap is a `.claude/skills/diagnostics.md` wrapper for ergonomic invocation.

5. **Graphify** — Correctly deferred. No gap.

**The one concrete action:** Create `.claude/skills/diagnostics.md`. Everything else requires agent-level implementation per the roadmap sequence — which is blocked pending Episode Zero completion, not by missing skill files.

---

## 10. Reconciliation: Roadmap vs Repository State

| Roadmap Item | Roadmap Says | Repository Has | Status |
|---|---|---|---|
| Humanizer | Installed, active, done | `hypogeum/humanizer.py` | **Reconciled — complete** |
| Seedance2 | Deferred, no slash-command | Nothing (correct) | **Reconciled — deferred correctly** |
| Journalism / SIFT | Deferred, no slash-command | Nothing (correct) | **Reconciled — deferred correctly** |
| Graphify | Deferred, evaluate later | Nothing (correct) | **Reconciled — deferred correctly** |
| Brand Guardian | M10.1 — full agent | Spec docs only, no code | **Gap — agent not implemented** |
| Finance Controller | M7.1 — full agent | M5.6 guardrails (different scope) | **Gap — M7.1 not implemented** |
| Asset Archive | M7.2 + M7.3 — full agents | Asset directory only, no agent | **Gap — agents not implemented** |
| Quality Assurance | M9.1 — full agent | Not implemented | **Gap — agent not implemented** |
| Rights & Compliance | M9.2 — full agent | Not implemented | **Gap — agent not implemented** |
| Provider Diagnostics | Scripts exist, not a roadmap item | Scripts exist, no skill wrapper | **Minor gap — skill wrapper only** |
| `docs/channel-identity.md` | M10.2 — document required first | Does not exist | **Gap — document not created** |

**Note on initial conclusion:** The first audit classified skills primarily as "missing implementation." This misses the operational point. The correct framing is in Section 11 below.

---

## 11. Revised Framework: Five Artifact Types

The initial audit treated skills as a subset of implementation gaps. That framing is wrong.

The correct distinction is:

| Type | What It Does | Prevents |
|---|---|---|
| **Runtime code** | Executes logic in the production pipeline | N/A — it is the pipeline |
| **Agent implementation** | A full Octogent agent with Python + schemas + prompts + tests | Missing capability |
| **Vault knowledge** | A reference document for operator or agent context | Information loss |
| **Agent prompt policy** | Instructions baked into a system prompt file | Agent behavioral drift within a run |
| **Claude Code skill** | A repeatable operating procedure invokable across sessions | **Operator behavioral drift across sessions** |

**The problem skills solve is not missing functionality. It is behavioral drift.**

Without skills:
- Rules are sometimes followed, sometimes missed
- Claude reads different files in different sessions
- Inferred values get treated as verified ones
- Budget constraints get forgotten mid-session
- Procedures get partially completed then diverge

A Claude Code skill is a **session-invariant operating procedure**. It enforces that every time a specific operation is performed, the same checks happen in the same order against the same source files.

---

## 12. Folder Convention for Claude Code Skills

Skills are stored as markdown files in:

```
.claude/skills/<skill-name>.md
```

Each file is a prompt template invokable via `/skill-name` at the Claude Code prompt. The Skill tool executes them.

**The project currently has no `.claude/skills/` directory.** This directory must be created before any skill files can be added.

**Naming convention for this project:**

- Use lowercase, hyphen-separated names
- Prefix with the domain area for grouping: `provider-*`, `budget-*`, `brand-*`, `episode-*`, `visual-*`
- Keep names short and verb-led where possible: `diagnostics`, `budget-run`, `cost-forensic`

**What skills are not:**

- They are not Python scripts
- They are not agent system prompts
- They do not replace `hypogeum/agents/` implementations
- They do not modify source files or generated artifacts
- They enforce operating procedures; they do not execute production logic

---

## 13. Recommended Claude Code Skills

Ten skills are proposed. Each entry specifies: purpose, trigger/use case, files it must read, files it must not edit, expected output, and whether it is needed now or deferred.

---

### SK-01 — Provider Diagnostics

**Skill name:** `diagnostics`  
**Invoked as:** `/diagnostics`

**Purpose:**  
Run pre-flight provider validation before any generation session. Confirms credentials are present, providers are reachable, and known cost benchmarks are on record. Prevents starting a session with stale or missing configuration.

**Trigger / use case:**
- Before any session that will invoke `run_pipeline.py`
- After changing any credential in `.env`
- After a provider error (402, 401, timeout) to confirm whether it is credential or balance issue
- At the start of any session touching MUAPI or ElevenLabs

**Files it must read:**
- `.env.example` — to know which keys are required
- `scripts/validate_credentials.py` — to understand what is checked
- `scripts/runway_diagnostic.py` — Runway connectivity and cost validation
- `scripts/seedance_diagnostic.py` — Seedance connectivity and cost validation
- `scripts/runway_diagnostic_result.json` — last recorded Runway cost benchmark
- `scripts/seedance_diagnostic_result.json` — last recorded Seedance cost benchmark
- `.octogent/tentacles/provider-integrations/provider-architecture.md` — provider capability matrix

**Files it must NOT edit:**
- `.env` — never modify credentials
- Any diagnostic result JSON — read only; results are written by the scripts themselves

**Expected output:**
- Credential presence status per provider (PRESENT / MISSING)
- Last recorded cost benchmarks (Runway: $0.15/clip, Seedance: $0.675/5s)
- Instruction to run `python3 scripts/validate_credentials.py` if credential gaps found
- Go / No-Go verdict before proceeding

**Needed now or deferred:** **NOW** — should be the first skill created. Prevents repeating the MUAPI quota exhaustion scenario.

---

### SK-02 — Budget-Safe Generation Run

**Skill name:** `budget-run`  
**Invoked as:** `/budget-run`

**Purpose:**  
Enforce the correct flag sequence before any pipeline invocation that will call MUAPI. Confirms dry-run passes before approving real spend. Prevents silent overspend and quota exhaustion.

**Trigger / use case:**
- Before any invocation of `run_pipeline.py` that calls MUAPI (i.e., any run not in `--dry-run` mode)
- When resuming a stalled episode after a quota error
- When changing `MUAPI_EPISODE_BUDGET_CREDITS` or `MUAPI_CHEAP_MODE`

**Files it must read:**
- `hypogeum/config.py` — to identify the exact env var names and defaults:
  - `MUAPI_DRY_RUN` (default: false)
  - `MUAPI_CHEAP_MODE` (default: false)
  - `MUAPI_ALLOW_PAID_FALLBACKS` (default: false)
  - `MUAPI_EPISODE_BUDGET_CREDITS` (default: 0 = unlimited — dangerous)
  - `MUAPI_SCENE_BUDGET_CREDITS` (default: 0 = unlimited)
- `scripts/runway_diagnostic_result.json` — confirmed per-clip costs
- `scripts/seedance_diagnostic_result.json` — confirmed per-clip costs
- `.hypogeum-assets/episodes/<episode_id>/visual_package_<episode_id>.json` — scene count and tier routing

**Files it must NOT edit:**
- `hypogeum/config.py`
- `.env`
- Any generated artifact

**Expected output:**
- Ordered checklist:
  1. Confirm `MUAPI_DRY_RUN=true` is set → run pipeline → confirm dry-run completes without error
  2. Confirm scene count and tier routing from Visual Package
  3. Calculate estimated cost: scene count × tier costs from diagnostic results
  4. Confirm `MUAPI_EPISODE_BUDGET_CREDITS` is set to a non-zero ceiling
  5. Confirm `MUAPI_ALLOW_PAID_FALLBACKS=false` unless explicitly required
  6. Only after all items pass: authorize switching `MUAPI_DRY_RUN=false`
- Go / No-Go verdict for real run

**Needed now or deferred:** **NOW** — this is the exact failure mode that blocked Episode Zero.

---

### SK-03 — Cost Forensic Investigation

**Skill name:** `cost-forensic`  
**Invoked as:** `/cost-forensic`

**Purpose:**  
When a pipeline run ends with a credit error, budget exceeded, or unexpected spend, trace exactly what was called, at what tier, and what it cost — before deciding to top up, change tier routing, or reroute scenes.

**Trigger / use case:**
- After any MUAPI 402 (insufficient credits) error
- When actual spend is higher than expected
- Before deciding whether to top up a provider account
- When reviewing whether MUAPI T1/T2/T3 routing decisions are cost-appropriate

**Files it must read:**
- `hypogeum/muapi.py` — provider integration (understand what is called and how)
- `hypogeum/config.py` — budget flag defaults
- `.hypogeum-assets/episodes/<episode_id>/visual_package_<episode_id>.json` — scene list with tier assignments
- `scripts/runway_diagnostic_result.json` — confirmed Runway cost ($0.15/clip)
- `scripts/seedance_diagnostic_result.json` — confirmed Seedance cost ($0.675/5s clip)
- `.octogent/tentacles/provider-integrations/provider-architecture.md` — full tier cost matrix
- `docs/provider-decisions.md` — rationale for tier routing decisions

**Files it must NOT edit:**
- Any visual_package JSON
- Any provider integration source file
- Any diagnostic result

**Expected output:**
- Scene-by-scene cost breakdown: episode_id, scene index, provider, model, tier, estimated cost (USD)
- Total projected cost for the episode at current tier routing
- Comparison against `MUAPI_EPISODE_BUDGET_CREDITS` ceiling
- Flag: which scenes are T1 (veo3, $2.50/8s) vs T2 (runway, $0.15/clip) vs T3 (veo3-fast, lowest)
- Recommendation: change tier routing, reduce scene count, or proceed with top-up

**Needed now or deferred:** **NOW** — Episode Zero failed on a 402. This skill prevents guessing the cause in the next session.

---

### SK-04 — Episode Zero Lite Run Procedure

**Skill name:** `ep0-run`  
**Invoked as:** `/ep0-run`

**Purpose:**  
Step-by-step operator checklist before invoking `run_pipeline.py` for Episode Zero. Prevents running with wrong flags, unverified credentials, or without completing pre-flight steps. This is the authoritative procedure for resuming or completing Episode Zero.

**Trigger / use case:**
- Any session that intends to resume or complete Episode Zero production
- After MUAPI account is topped up
- After any credential change that may affect Episode Zero

**Files it must read:**
- `docs/episode-zero-status.md` — current completion state of each artifact
- `docs/first-production-run.md` — original operator checklist
- `docs/production-onboarding-plan.md` — setup requirements
- `memory/project_state.md` — current operational priority and blocking factors
- `.hypogeum-assets/episodes/ep_000/` — inventory of already-generated artifacts (do not regenerate what exists)
- `.env.example` — confirm all required keys

**Files it must NOT edit:**
- `.hypogeum-assets/episodes/ep_000/*.json` — generated artifacts are read-only in this procedure
- `docs/episode-zero-status.md` — update only after completion, not during procedure

**Expected output:**
- Ordered checklist with gate conditions:
  1. Run `/diagnostics` — confirm all providers reachable
  2. Run `/budget-run` — confirm cost ceiling set, dry-run passes
  3. Confirm which artifacts already exist (skip completed stages)
  4. Confirm YouTube OAuth token is valid (out-of-band step)
  5. Confirm MUAPI account has sufficient balance for remaining scenes
  6. Invoke: `python3 run_pipeline.py --topic "The Antikythera Mechanism" --episode-id ep_000 --youtube-privacy private`
- No-Go conditions that must block execution

**Needed now or deferred:** **NOW** — Episode Zero is blocked and will be resumed in an upcoming session.

---

### SK-05 — Artifact and Log Collection

**Skill name:** `collect`  
**Invoked as:** `/collect [episode_id]`

**Purpose:**  
After any pipeline run (complete or partial), inventory all generated artifacts for an episode — present vs expected — and surface any anomalies before proceeding with analysis or remediation. Prevents operating on incomplete assumptions about what was actually produced.

**Trigger / use case:**
- After any pipeline run (success or failure)
- When resuming a stalled episode
- Before deciding whether an episode is complete
- Before running any downstream operation (publishing, QA, brand review)

**Files it must read:**
- `.hypogeum-assets/episodes/<episode_id>/` — all files in the episode directory
- `hypogeum/schemas.py` — expected output schemas per agent (to know what fields must be present)
- `docs/episode-zero-status.md` — for Episode Zero specifically

**Files it must NOT edit:**
- Any artifact file in `.hypogeum-assets/`

**Expected output:**
- Artifact inventory table: filename, size, exists (yes/no), non-empty (yes/no)
- Expected vs present: which schemas are complete vs missing
- Anomaly flags: zero-byte files, truncated JSON, missing required schema fields
- Stage completion assessment: which pipeline stages are done, which are partial, which are not started
- Next step recommendation

**Needed now or deferred:** **NOW** — Episode Zero has a mix of complete and incomplete artifacts. The actual completion state of some files (e.g., `final_render_package_ep_000.json`, `publishing_record_ep_000.json`) is not verified.

---

### SK-06 — Brand Bible Extraction Validation

**Skill name:** `brand-extract`  
**Invoked as:** `/brand-extract`

**Purpose:**  
When reading or updating brand values from any source, classify each value by confidence level before treating it as enforceable. Prevents the known failure mode of promoting inferred or unverified values to enforced brand rules.

**Trigger / use case:**
- Any session touching `docs/brand_guardian/` files
- Before updating `brand_spec_executable.md`
- Before using any brand value (color, font, size, position) as a constraint in production
- When the Brand Guardian Agent (M10.1) is eventually implemented

**Files it must read:**
- `docs/brand_guardian/brand_bible_hypogeum.md` — primary source of truth
- `docs/brand_guardian/extraction_audit.md` — prior extraction work and confidence levels
- `docs/brand_guardian/gap_report.md` — known gaps and unresolved values
- `docs/brand_guardian/brand_spec_executable.md` — current executable spec (verify against audit)
- `docs/brand_guardian/brand_bible_schema.json` — schema for the brand bible

**Files it must NOT edit:**
- `docs/brand_guardian/brand_bible_hypogeum.md` — never modify the source
- `docs/brand_guardian/brand_bible_schema.json` — frozen schema

**Expected output:**
- Per-field confidence classification table:
  - `VERIFIED` — confirmed from brand bible source with direct citation
  - `HIGH_CONFIDENCE` — strongly implied, consistent across multiple references
  - `INFERRED` — derived from context, not stated directly
  - `NOT_VERIFIED` — present in spec but not confirmed from source
  - `MISSING` — required by schema but absent from all sources
- List of values that must not be used as production constraints until verified
- Specific: current memory states hex codes as `NOT_VERIFIED` — this skill must surface that before any color enforcement

**Needed now or deferred:** **NOW** — the memory explicitly records that hex codes are NOT_VERIFIED. This skill prevents promoting them to enforced constraints.

---

### SK-07 — BGA Preflight Review

**Skill name:** `bga-preflight`  
**Invoked as:** `/bga-preflight [episode_id]`

**Purpose:**  
Before any episode reaches publishing, manually execute the Brand Guardian validation checklist using existing spec documents. This is the human-in-the-loop substitute for the M10.1 Brand Guardian Agent, which does not yet exist.

**Trigger / use case:**
- Before publishing any episode
- Before approving thumbnail outputs from Thumbnail Director
- When reviewing any visual artifact that carries brand-identity elements (thumbnails, graphics, captions)
- Must run before M10.1 Brand Guardian Agent exists

**Files it must read:**
- `docs/brand_guardian/brand_spec_executable.md` — the enforcement checklist
- `docs/brand_guardian/brand_bible_hypogeum.md` — authoritative source for disputed values
- `docs/brand_guardian/gap_report.md` — known gaps that cannot be validated yet
- `docs/brand_guardian/extraction_audit.md` — confidence levels (must not enforce NOT_VERIFIED values)
- `.hypogeum-assets/episodes/<episode_id>/thumbnail_package_<episode_id>.json` — thumbnail spec
- `.hypogeum-assets/episodes/<episode_id>/thumbnail_generation_plan_<episode_id>.json` — generation plan

**Files it must NOT edit:**
- Any brand_guardian spec file
- Any thumbnail artifact

**Expected output:**
- Per-dimension compliance table:
  - Fonts: family, weight, size — PASS / WARNING / VIOLATION / CANNOT_VERIFY
  - Colors: primary, secondary, accent — PASS / WARNING / VIOLATION / CANNOT_VERIFY
  - Subtitle style: position, size, weight, stroke, shadow — PASS / WARNING / VIOLATION / CANNOT_VERIFY
  - Thumbnail composition: hierarchy, text placement, forbidden elements — PASS / WARNING / VIOLATION / CANNOT_VERIFY
  - Title format: character limits, style conventions — PASS / WARNING / VIOLATION / CANNOT_VERIFY
- Explicit notation of which validations are blocked by NOT_VERIFIED source values
- Overall verdict: APPROVED / CONDITIONAL / BLOCKED

**Needed now or deferred:** **NOW** — the BGA Python agent (M10.1) is deferred. Publishing will happen before it exists. This skill is the only brand governance that will be available at first publish.

---

### SK-08 — Documentary Visual Grammar Review

**Skill name:** `visual-review`  
**Invoked as:** `/visual-review [episode_id]`

**Purpose:**  
Review a completed Visual Package against documentary filmmaking conventions before submitting scenes to MUAPI. Checks for shot monotony, coverage diversity, era-appropriate treatment, and whether provider tier assignments are rational given scene importance.

**Trigger / use case:**
- After VPD produces a `visual_package_*.json`
- Before submitting scenes to MUAPI for generation
- When reviewing `visual_package_ep_000.json` (46 scenes, 95KB — not yet reviewed against documentary grammar)

**Files it must read:**
- `hypogeum/prompts/visual_package_director.txt` — VPD instructions (what it was told to produce)
- `.octogent/tentacles/visual-production/CONTEXT.md` — continuity lock model, text-safe zones
- `.hypogeum-assets/episodes/<episode_id>/visual_package_<episode_id>.json` — the package being reviewed
- `.octogent/tentacles/provider-integrations/provider-architecture.md` — tier cost and capability matrix
- `.hypogeum-assets/episodes/<episode_id>/narrative_package_<episode_id>.json` — narrative structure (for scene-to-beat alignment)

**Files it must NOT edit:**
- `visual_package_*.json` — read only; revisions are flagged for human decision, not silently applied
- `hypogeum/prompts/visual_package_director.txt` — frozen

**Expected output:**
- Shot type distribution: wide / medium / close-up / insert — flag if >60% one type
- Camera motion distribution: static / pan / push / pull / handheld — flag monotony
- Tier assignment audit: which scenes are T1 ($2.50), T2 ($0.15), T3 (lowest) — confirm high-cost tiers are justified by narrative importance
- Continuity check: entity consistency across scenes (character locks, location locks)
- Coverage gaps: are all narrative beats covered by at least one scene?
- Text-safe zone compliance: Shorts scenes must have declared safe zones
- Recommended revisions list (flagged, not applied)
- Overall verdict: APPROVED / REVISE / BLOCKED

**Needed now or deferred:** **NOW** — `visual_package_ep_000.json` exists (95KB, 46 scenes) and has not been reviewed. Scenes will be submitted to MUAPI in the next production session.

---

### SK-09 — Source Verification / SIFT Workflow

**Skill name:** `sift`  
**Invoked as:** `/sift [episode_id]`

**Purpose:**  
When reviewing research claims in a Research Package or Script Package, apply the SIFT methodology (Stop, Investigate the source, Find better coverage, Trace claims) systematically. Prevents treating unverified or weakly-sourced claims as established fact in narration.

**Trigger / use case:**
- Before approving a Research Package for Narrative Architect processing
- When a claim feels undersubstantiated or contradicts prior knowledge
- When verifying a controversial historical assertion in the Script Package
- When the Research Director has surfaced explicit contradictions in the Research Package

**Files it must read:**
- `hypogeum/prompts/research_director.txt` — what the RD was instructed to do re: source quality
- `.hypogeum-assets/episodes/<episode_id>/research_package_<episode_id>.json` — claims and sources
- `.hypogeum-assets/episodes/<episode_id>/script_package_<episode_id>.json` — narration (to catch unsourced claims that made it downstream)

**Files it must NOT edit:**
- `research_package_*.json` — read only; flags are reported, not silently corrected
- `script_package_*.json` — same

**Expected output:**
- Per-claim verification table: claim text, stated source, SIFT step applied, verification status (VERIFIED / PARTIALLY_VERIFIED / UNVERIFIED / CONTRADICTED)
- Source quality assessment per citation (primary / secondary / popular press / unattributed)
- Flagged claims requiring additional sourcing before narration can proceed
- Summary: total claims, verified count, unverified count, contradicted count

**Needed now or deferred:** **DEFERRED** — Episode Zero research and script are already complete and locked. This skill is most valuable for episodes 2+, where research review happens before script locks. Create after Episode Zero is complete.

---

### SK-10 — Seedance Prompt Writing

**Skill name:** `seedance-prompt`  
**Invoked as:** `/seedance-prompt`

**Purpose:**  
When writing or reviewing VPD scene briefs, apply documentary cinematographic vocabulary precisely: shot size, camera motion, depth of field, lighting grammar, and period-appropriate atmosphere. Prevents vague or technically incoherent scene descriptions that produce poor generation results.

**Trigger / use case:**
- When writing scene briefs for a new episode
- When reviewing `visual_package_*.json` and scenes feel visually under-specified
- When preparing to regenerate specific scenes after a production review
- When the `/visual-review` skill flags shot monotony or vague descriptions

**Files it must read:**
- `hypogeum/prompts/visual_package_director.txt` — VPD vocabulary and constraints
- `.octogent/tentacles/visual-production/CONTEXT.md` — scene brief schema and continuity rules
- `.octogent/tentacles/provider-integrations/provider-architecture.md` — provider capability per model (what Veo3 vs Runway can render)
- `.hypogeum-assets/episodes/<episode_id>/visual_package_<episode_id>.json` — scenes to enrich (if reviewing existing package)

**Files it must NOT edit:**
- `hypogeum/prompts/visual_package_director.txt` — frozen
- `visual_package_*.json` — enriched scenes are proposed, not silently written

**Expected output:**
- Per-scene enriched brief with: shot_size (ECU/CU/MCU/MS/MWS/WS/EWS), camera_motion (static/pan/tilt/push/pull/crane/handheld), depth_of_field (shallow/deep/rack), lighting_quality (golden-hour/overcast/candlelight/torchlight/harsh-direct/soft-diffuse), period_atmosphere (specific era-appropriate descriptor), motion_intensity (none/subtle/moderate/dynamic)
- Note: these are proposed enrichments, not authoritative overrides — VPD owns the final brief

**Needed now or deferred:** **DEFERRED** — most valuable after reviewing Episode Zero visual output and identifying where descriptions were too vague. Create after first MUAPI generation results are available.

---

## 14. Skills Priority Summary

| Skill | Name | Needed Now | Reason |
|---|---|---|---|
| SK-01 | `diagnostics` | **YES** | First line of defense before any generation session |
| SK-02 | `budget-run` | **YES** | Prevents repeat of MUAPI 402 failure |
| SK-03 | `cost-forensic` | **YES** | Diagnose cost failures without guessing |
| SK-04 | `ep0-run` | **YES** | Authoritative procedure for Episode Zero resumption |
| SK-05 | `collect` | **YES** | Verify artifact state before proceeding |
| SK-06 | `brand-extract` | **YES** | Hex codes are NOT_VERIFIED — must not be enforced |
| SK-07 | `bga-preflight` | **YES** | Only brand governance available before M10.1 exists |
| SK-08 | `visual-review` | **YES** | `visual_package_ep_000.json` not yet reviewed |
| SK-09 | `sift` | Deferred | Research for Episode Zero already locked |
| SK-10 | `seedance-prompt` | Deferred | Useful after first generation results are reviewed |

**Immediate action (pending approval):** Create `.claude/skills/` directory and the 8 NOW-priority skill files.

**No implementation beyond skill files** — these skills reference existing code and artifacts. They do not create new agents, modify schemas, or alter pipeline behavior.
