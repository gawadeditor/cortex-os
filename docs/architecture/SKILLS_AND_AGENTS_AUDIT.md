# Skills and Agent Capability Mapping — Read-Only Audit

**Date:** 2026-07-10
**Scope:** Skills Standardization Phase — audit only. No files modified, no agents changed, no skills installed.
**Method:** Graphify-first (`graphify query`) for orientation, followed by targeted `Read`/`grep` on files Graphify surfaced, per `docs/architecture/GRAPHIFY_FIRST_WORKFLOW.md`.
**Gate satisfied:** This audit begins only after `GRAPHIFY_FIRST_WORKFLOW.md` was committed (§8 of that document), and after `GRAPHIFY_VERIFICATION_REPORT.md` confirmed the graph is READY for CLI retrieval.
**Constitution check:** Per `PROJECT_CONSTITUTION.md` and `PROJECT_PRINCIPLES.md`, this work sits at priority #3 ("Skill architecture redesign") — architecture-before-features, manual-validation-first. This document performs no implementation, per `CURRENT_PHASE.md` ("Skill redesign" is current work; this is the audit precursor to it).

**Graph staleness note:** `graphify-out/graph.json` was built 2026-07-10 01:21:42. Two post-build commits moved files into `_archive/stale-untracked-2026-07-10/` (e.g. `experiments/skill_prompt_compare_v1.py`, `scripts/recomposite_ep003.py`). Graphify query results below still show pre-move paths for those files; this audit uses the corrected on-disk paths, confirmed by direct `ls`/`grep`, not the stale graph paths. Everything else below (agents, skills, config) was unaffected by those two commits and is current.

---

## Correction Note (post-audit, same date)

The original §5 finding below ("8 skills present in both `~/.claude/skills/<name>/SKILL.md` and `.claude/skills/<name>.md`, byte-for-byte matching") was refined by an actual sha256 verification pass, not just the `wc -c` byte-count comparison this audit originally relied on:

- **6 of the 8 were confirmed byte-identical** (`bga-preflight`, `budget-run`, `collect`, `cost-forensic`, `diagnostics`, `brand-extract`) — sha256 matched exactly between the project flat file and the global `SKILL.md`. These 6 project-level duplicate files have been **removed**. Note for future audits: none of these 6 were ever tracked by git — `.claude/skills/*` is gitignored except the `graphify` subtree (see `.gitignore` lines 15–25) — so their removal produces no `git diff`/`git status` entry; the global `SKILL.md` copies (also outside this repo, under `~/.claude/skills/`) remain the sole surviving copy of each.
- **2 of the 8 were NOT byte-identical** (`visual-review`, `ep0-run`) — the project-level flat copies contain newer/different content than the global versions (e.g. `ep0-run.md` project copy has an added checkpoint-file table; `visual-review.md` project copy has an added "Attachment Note" section not present globally). These are **not duplicates** and were **not touched**. They require a later explicit merge/source-of-truth decision (which version is canonical, whether to reconcile content into one location) — do not delete either copy automatically.

---

## Follow-Up Note (post-audit, 2026-07-10) — visual-review / ep0-run resolved

A subsequent read-only investigation confirmed the two non-identical pairs flagged above (`visual-review`, `ep0-run`) were not ambiguous: in both cases the project copy was newer and strictly more accurate — for `ep0-run` specifically, the global copy contained a stale factual claim ("the pipeline does not currently checkpoint") contradicted by live checkpoint/resume logic confirmed in `hypogeum/pipeline.py`. On that basis:

- **`.claude/skills/visual-review.md` and `.claude/skills/ep0-run.md` (project copies) were confirmed canonical.**
- **The global `SKILL.md` copies were synchronized from the project copies** — `~/.claude/skills/visual-review/SKILL.md` and `~/.claude/skills/ep0-run/SKILL.md` were overwritten to match the project versions exactly (sha256-verified identical after sync). No content was authored or edited by hand; the global files now simply mirror the already-canonical project files.
- **Backups of the pre-sync global files were created** at `~/.claude/skills/visual-review/SKILL.md.backup-2026-07-10` and `~/.claude/skills/ep0-run/SKILL.md.backup-2026-07-10` before either overwrite.
- **The repo working tree remained clean throughout this sync** — `~/.claude/skills/` is outside this repository entirely (a different, global directory under the user's home folder, not `.claude/skills/` inside this repo), so neither the overwrite nor the backup files produce any `git status`/`git diff` entry here. Only this documentation update is a repo-tracked change.

---

## 1. Existing Skills Inventory

### 1a. Internal agent skills — `hypogeum/skills/` (Python-loaded, injected into LLM system prompts)

| File | Origin | Status | Size (full / compact) | Token-risk | Disposition |
|---|---|---|---|---|---|
| `visual_package_director/cinematic_prompt_architecture.md` | internal | **wired** — VPD Stage A (legacy) + Stage B | 11,975 chars / no compact | Medium — largest single contributor to VPD Stage B prompt (~10K+ of ~35K chars) | Remain as-is. Compaction was evaluated in B1 and explicitly deferred (R-02, `SKILL_TOKEN_BUDGET_REPORT.md`) — it's a layer-by-layer prescriptive spec; cutting risks silently dropping a required layer. Highest-value future compaction target if budget pressure returns. |
| `visual_intelligence/visual_language_engine.md` | internal | **wired** — VPD Stage A4 + Shot Diversity Planner | 11,061 chars full / 5,465 compact | Low (compact is default) | Remain. Full retained for human audit only. |
| `veo_translation_skill.md` | internal | **wired** — VPD Stage B | 65,463 chars full / 7,285 compact | Low (compact is default); full version is the single largest file in the skill tree | Remain. Full retained for reference; compact is what agents actually load. |
| `seedance_translation_skill.md` | internal | **wired** — VPD Stage B | 57,020 chars full / 8,501 compact | Low (compact default) | Remain. |
| `visual_intelligence/thumbnail_intelligence_pipeline.md` | internal | **wired** — `thumbnail_prompt_writer` Phase 1 | 4,469 chars full / 2,418 compact | Low | Remain. |
| `provider_skill_template.md` | internal (governance) | **governance-only** — never injected | 17,974 chars | None (not loaded at runtime) | Remain. Meta-document for humans authoring new provider skills; correctly excluded from agent prompts per `skill_registry.py`. |
| `veo_prompt_skill.md` | internal (v1.0) | **superseded** by `veo_translation_skill.md` v3.0 | 60,431 chars | None (not loaded) | Candidate for removal later — explicitly flagged as stale-guidance risk in `SKILL_WIRING_REPORT.md` R-04 ("if a future developer adds a new import referencing it by name, they will get stale guidance"). Not removed now — audit only. |
| `thumbnail_strategy.py` | internal | **wired but structurally misplaced** | n/a (code, not markdown) | n/a | This is a full LLM-calling Python module (`run_agent()`, `model_slug=config.OPENROUTER_SONNET_MODEL_ID`) living under `hypogeum/skills/` alongside markdown reference skills. It is not a skill by this repo's own definition (skills are static reference documents loaded via `compose_system_prompt()`; this file *makes its own LLM call*). See §5 for the miswiring finding. |

### 1b. Project-level user-facing skills — `.claude/skills/*.md` (flat files, Claude Code runtime, no YAML frontmatter)

| File | Origin | Status | Size | Token-risk | Disposition |
|---|---|---|---|---|---|
| `diagnostics.md` | internal | wired (dashboard operator workflow) | 4,171 B | Low | Remain |
| `publishing-preflight.md` | internal | wired | 4,500 B | Low | Remain |
| `budget-run.md` | internal | wired | 4,769 B | Low | Remain |
| `cost-forensic.md` | internal | wired | 5,795 B | Low | Remain |
| `frontend_polish.md` | internal | wired | 5,924 B | Low | Remain |
| `brand-extract.md` | internal | wired | 6,140 B | Low | Remain |
| `ep0-run.md` | internal | wired | 6,278 B | Low | Remain |
| `dashboard_ux.md` | internal | wired | 6,371 B | Low | Remain |
| `collect.md` | internal | wired | 6,518 B | Low | Remain |
| `information_architecture.md` | internal | wired | 6,852 B | Low | Remain |
| `ui_ux_audit.md` | internal | wired | 6,853 B | Low | Remain |
| `analytics_experience_design.md` | internal | wired | 7,051 B | Low | Remain |
| `bga-preflight.md` | internal | wired | 7,392 B | Low | Remain |
| `data_visualization.md` | internal | wired | 7,921 B | Low | Remain |
| `visual-review.md` | internal | wired | 8,683 B | Low | Remain |
| `graphify/SKILL.md` (dir, with `references/` subtree + `.graphify_version`) | **upstream** — `graphify` CLI tool's own installed skill (`graphify install`) | wired, ACTIVE per `GRAPHIFY_FIRST_WORKFLOW.md` | 38,064 B | Low-Medium (single largest project skill) | Remain as-is — this is upstream-installed by the `graphify` tool itself, not hand-authored. Do not edit or reconstruct; if it needs updating, re-run the tool's own install/update path. |

All 15 flat `.md` files match the finding already recorded in `SKILL_LOADING_AUDIT.md` (M-01): standard `SKILL.md`-in-named-directory format with YAML frontmatter is not used here. Functionally they all work (Claude Code falls back to filename-based identification), but descriptions shown in the system-reminder skill list are less precise than a curated `description:` field would give. This repo's own CLAUDE.md (`.claude/CLAUDE.md`) confirms only `graphify` is currently registered as a proper frontmatter skill among project-level skills.

### 1c. Global user-facing skills — `/Users/gawadeditor/.claude/skills/` (apply across all projects, not Hypogeum-specific)

| Group | Files | Origin | Status | Size | Disposition |
|---|---|---|---|---|---|
| `01-cinematic` … `15-real-estate` (15 files) | **unknown** — Higgsfield/Seedance 2.0 prompt-generation skills. Descriptions and content are scoped to the `claude_ai_higgsfield` MCP server's product surface ("Seedance 2.0 on Higgsfield"), not to Hypogeum. No upstream URL recorded anywhere in this repo. | wired (global), **unused by Hypogeum pipeline** | 42–95 KB each; 1.15 MB combined across all 24 global skills | These are large, general-purpose, product-specific to a different tool (Higgsfield video generation via MCP) and structurally unrelated to the Hypogeum agent pipeline (which uses OpenRouter/MUAPI/ElevenLabs, not Higgsfield). They do not appear in any Hypogeum skill-loading code path. Out of scope for Hypogeum skill standardization — flag as external/unrelated, do not touch. |
| `bga-preflight`, `budget-run`, `collect`, `cost-forensic`, `diagnostics`, `brand-extract`, `visual-review`, `ep0-run` (8 files) | internal — **duplicates of the project-level `.claude/skills/*.md` files**, but in proper `SKILL.md`-in-directory format with frontmatter | wired (global) — shadows or duplicates the project-level flat versions | 4.2–8.0 KB each | These are the same 8 skills that also exist as flat files in `.claude/skills/`. This is a real duplication: two copies of the same skill content in two formats at two scopes. See §5. |
| `harness` | internal (Korean-language meta-skill for agent-team scaffolding) | wired (global), not used by Hypogeum pipeline | 29,830 B | Not part of Hypogeum's factory; a general-purpose Claude Code meta-skill. Out of scope. |
| `remotion` | upstream (Remotion best-practices reference) | wired (global) | 11,678 B | Out of scope — general React/video-in-code reference, not part of Hypogeum's pipeline. |
| `roughcut` | internal/unknown (Buttercut gem roughcut YAML generator) | wired (global) | 2,853 B | Out of scope for this audit's agent-skill fit question; unrelated to the LLM agent pipeline. |

---

## 2. Skill Loading Architecture

Two entirely independent loading systems exist, confirmed unchanged since `SKILL_LOADING_AUDIT.md` (2026-07-08):

**A. Claude Code runtime skill system** (`.claude/skills/`, global `~/.claude/skills/`) — discovered at session start, static, re-read from disk on every `/skill-name` or `Skill` tool invocation, injected into conversation context only for that turn. No caching, no persistent system-prompt residency. Controlled entirely by the Claude Code harness, not by any Hypogeum Python code.

**B. Internal agent skill system** (`hypogeum/skills/` + `hypogeum/prompt_loader.py` + `hypogeum/skill_registry.py`) — discovered via hard-coded `Path` constants in agent modules at Python import time, loaded once per process into module-level string constants, injected as the `system=` argument to every `run_agent()` call for that agent for the remainder of the process lifetime. No hot-reload.

**Registry/loader control:**
- `hypogeum/prompt_loader.py` — the only I/O layer: `load_prompt()` (base prompts, strips `# KEY: VALUE` headers), `load_skill()` (skills, verbatim, returns `""` silently if missing), `compose_system_prompt()` (joins with `"\n\n---\n\n"`).
- `hypogeum/skill_registry.py` — authoritative metadata registry for all 7 internal skill entries (path, compact_path, status, wired_to, rationale, char counts). Does not itself load anything — agents call `compose_system_prompt()` directly with paths; `skill_registry` is bookkeeping plus the `log_loaded_skills()` diagnostic emitter.
- `hypogeum/prompt_registry.py` — a **third, separate, read-only introspection layer** over `hypogeum/prompts/*.txt` (base prompts, not skills). Provides `list_prompts()` / `get_prompt()` / `get_prompt_metadata()` for the dashboard and `hypogeum/model_comparison.py` to display prompt metadata (`PROMPT_VERSION`, `OWNER_AGENT`, `MODEL_TARGET` headers). It is never called by any agent at runtime — confirmed by `grep`: only `model_comparison.py` and `prompt_registry.py` itself reference it. It plays no role in what gets sent to the LLM.

**Which agents receive which skills** (internal system only — see §3 table for full per-agent breakdown):

| Agent | Skills loaded |
|---|---|
| `visual_package_director.py` (Stage B) | `cinematic_prompt_architecture.md` (full) + `visual_language_engine.compact.md` + `veo_translation_skill.compact.md` + `seedance_translation_skill.compact.md` |
| `visual_package_director.py` (Stage A4) | `visual_language_engine.compact.md` |
| `visual_package_director.py` (Stage A legacy) | `cinematic_prompt_architecture.md` (full) |
| `shot_diversity_planner.py` | `visual_language_engine.compact.md` |
| `thumbnail_prompt_writer.py` | `thumbnail_intelligence_pipeline.compact.md` |
| All other LLM-calling agents (`executive_producer`, `research_director`, `narrative_architect`, `script_engine`, `brand_guardian_agent`, `thumbnail_identity_guardian`, `topic_ideator`) | **none** — base prompt only via `load_prompt()`, no `compose_system_prompt()` call |

**Model-aware or global?** Loading is **not model-aware**. `compose_system_prompt()` has no branch on which model the resulting string will be sent to — the same composed skill text goes to whichever model the agent's `model_slug` resolves to (Claude, GPT, etc., see §3). The Provider Capabilities context (`hypogeum/agents/scene_planner.py::PROVIDER_CAPABILITIES`, line 34) is the one place model/provider capability facts are injected as structured data rather than skill markdown — but it is not part of the skill-loading system; it's assembled separately into `build_scene_plan_input()`.

---

## 3. Agents Inventory

`hypogeum/agents/` contains 49 modules. Only 12 call an LLM directly (`run_agent()` → `hypogeum/agent.py` → `hypogeum/openrouter.py::call_claude()`); the remainder are deterministic/API-orchestration agents (FFmpeg, MUAPI, ElevenLabs, YouTube, file I/O, validation) with no skill-loading question at all — this pipeline pattern (agents named "X_agent.py" that are not LLM callers) matches CLAUDE.md's documented 9-stage pipeline where only some stages are LLM-authored.

### 3a. LLM-calling agents (skills are directly relevant)

| Agent | Role | Model (config var, default) | Prompt file | Skills currently loaded | Skills recommended later | Must NOT load |
|---|---|---|---|---|---|---|
| `executive_producer.py` | Scores topic ACCEPT/WARN/REJECT → `topic_brief` | `EP_MODEL`, default `anthropic/claude-sonnet-4.6` | `hypogeum/prompts/executive_producer.txt` | none | none identified — EP is a scoring/gating step, not a creative-writing step; skill injection risk outweighs benefit | Any visual/cinematic skill (irrelevant to scoring) |
| `research_director.py` | Perplexity queries → `research_package` | `RD_MODEL`, default `anthropic/claude-opus-4.6` | `hypogeum/prompts/research_director.txt` | none | none identified | Visual/cinematic skills |
| `narrative_architect.py` | Story structure → `narrative_package` | (uses `OPENROUTER_SONNET_MODEL_ID` family — verify exact var at implementation time, not hardcoded in this file's header comment) | `hypogeum/prompts/narrative_architect.txt` | none | none identified | Visual/cinematic skills |
| `script_engine.py` | Narration beats + visual directions → `script_package` | via `run_agent()`, base OpenRouter Sonnet slug | `hypogeum/prompts/script_engine.txt` | none | Possibly a lightweight visual-vocabulary reference (Script Engine writes "visual directions" consumed downstream by VPD) — **not confirmed necessary**, flag as open question rather than recommendation |  |
| `visual_package_director.py` | Scene planning (Stage A) + prompt writing (Stage B) → `visual_package` | Stage-dependent, OpenRouter Sonnet family | `hypogeum/prompts/visual_package_director.txt`, `vpd_skeleton*.txt`, `vpd_scene_presentation_planner.txt`, `vpd_scene_prompts.txt`, `vpd_visual_intent_qa.txt` | `cinematic_prompt_architecture.md`, `visual_language_engine.compact.md`, `veo_translation_skill.compact.md`, `seedance_translation_skill.compact.md` (stage-dependent, see §2 table) | Already fully wired per B0/B1 | Provider skills for providers not in the current tier routing |
| `shot_diversity_planner.py` | Assigns shot_scale/camera_movement/composition/visual_motif per scene | OpenRouter Sonnet family | `hypogeum/prompts/shot_diversity_planner.txt` | `visual_language_engine.compact.md` | Already resolved (was audit finding m-02, fixed in B0) | — |
| `thumbnail_prompt_writer.py` | Phase 1 GPT planning for thumbnails | `TD_PLANNING_MODEL`, default `openai/gpt-5.5-pro` | `hypogeum/prompts/thumbnail_director.txt` | `thumbnail_intelligence_pipeline.compact.md` | Already resolved (was audit finding m-01, fixed in B0) | — |
| `thumbnail_identity_guardian.py` | Claude-vision identity/consistency check on generated thumbnails | `TIG_MODEL`, default `anthropic/claude-sonnet-4.6` | `hypogeum/prompts/thumbnail_identity_guardian.txt` | none | none identified — QA/verification step, deliberately narrow | Cinematic/generative skills (would bias a verification step toward generation-style output) |
| `brand_guardian_agent.py` | Pass 1 (VPD visual scenes) + Pass 2 (thumbnail plans, visual identity) brand compliance | `BGA_PASS1_MODEL` (`anthropic/claude-sonnet-4.6`), `BGA_PASS2_MODEL` (`openai/gpt-5.5-pro`) | `hypogeum/prompts/brand_guardian_agent.txt` | none | **Candidate:** the brand bible content itself (once BGA is reactivated — currently frozen per memory `project_brand_guardian.md`) is conceptually a "skill" but is currently handled as a separate extraction artifact, not via `compose_system_prompt()`. Worth deciding explicitly when BGA work resumes. | Provider translation skills (BGA reviews composition/brand, not provider prompt syntax) |
| `discovery/topic_ideator.py` | LLM topic ideation (v2 discovery engine) | `OPENROUTER_SONNET_MODEL_ID` | `hypogeum/prompts/topic_ideator.txt` | none | none identified | Visual/cinematic/provider skills |
| `hypogeum/agents/thumbnail_strategy.py` | Standalone Phase-1 CTR-hypothesis generator, invoked manually (not yet wired into main pipeline per its own docstring) | `OPENROUTER_SONNET_MODEL_ID` (default `anthropic/claude-sonnet-4.6`) | `hypogeum/prompts/thumbnail_strategy.txt` | none | If/when wired into the pipeline between Script Engine and VPD (its own stated Phase 2 plan), it should probably receive `thumbnail_intelligence_pipeline.compact.md` for consistency with `thumbnail_prompt_writer.py` | — |
| `discovery_director.py` | Orchestrates `topic_ideator` + `source_verifier` + `queue_gateway` (no direct LLM call itself — delegates) | n/a (orchestrator) | n/a | n/a | n/a | n/a |

### 3b. Non-LLM agents (no skill question applies)

`archive_agent`, `assembly_agent`, `assembly_intelligence`, `asset_archive_agent`, `audio_agent`, `audio_qa`, `caption_agent`, `clip_repair_agent`, `compliance_agent`, `covered_topics_writer`, `editorial_qa`, `entity_continuity_manager`, `final_output_qa`, `final_render_reviewer`, `finance_agent`, `image_generation_agent`, `media_specs_qa`, `music_agent`, `provider_router`, `publishing_agent`, `qa_agent`, `reference_image_qa`, `thumbnail_composition_planner`, `thumbnail_compositor`, `thumbnail_generation_agent`, `thumbnail_handoff_brief_builder`, `thumbnail_killer_concept_validator`, `thumbnail_plan_validator`, `thumbnail_prompt_validator`, `thumbnail_qa`, `thumbnail_typography_planner`, `timeline_builder`, `timeline_qa`, `video_generation_agent`, `video_renderer`, `voice_generation_operator`, `hypogeum/discovery/source_verifier.py` (calls Perplexity directly, not `run_agent()`/Claude), `hypogeum/discovery/queue_gateway.py`, `hypogeum/discovery/repository.py`.

These wrap MUAPI (video/image gen), ElevenLabs (voice), FFmpeg (assembly), YouTube API (publishing), Perplexity (research retrieval), or perform deterministic validation/QA against schemas — no skill-loading question exists for them under the current architecture.

---

## 4. Agent-to-Skill Fit Matrix

| Category | Agents | Skills that fit | Skills that must NOT be loaded |
|---|---|---|---|
| **Prompt-writing agents** (narrative/script) | `narrative_architect`, `script_engine` | None currently wired; no strong case identified for adding any | Visual/cinematic/provider skills — wrong altitude, these agents don't write scene-level visual prompts |
| **Visual/video prompt agents** | `visual_package_director` (Stage A + B), `shot_diversity_planner` | `cinematic_prompt_architecture.md`, `visual_language_engine.compact.md`, `veo_translation_skill.compact.md`, `seedance_translation_skill.compact.md` — all correctly wired | Thumbnail-specific skills (`thumbnail_intelligence_pipeline`) — different deliverable, would add noise |
| **Thumbnail agents** | `thumbnail_prompt_writer`, `thumbnail_identity_guardian`, `hypogeum/agents/thumbnail_strategy.py` | `thumbnail_intelligence_pipeline.compact.md` (writer only; TIG is deliberately skill-free as a QA gate) | Provider video-translation skills (Veo/Seedance) — thumbnails are image generation, not video |
| **Research/script agents** | `research_director`, `executive_producer`, `topic_ideator` | None — these are scoring/retrieval/ideation steps upstream of any visual vocabulary need | Any cinematic/provider/thumbnail skill |
| **Dashboard/UI agents** | Not Python "agents" at all — `run_dashboard.py` + `hypogeum/dashboard_*.py` modules are Flask routes, not LLM callers. The relevant "skills" for this category are the Claude-Code-facing `.claude/skills/dashboard_ux.md`, `ui_ux_audit.md`, `information_architecture.md`, `analytics_experience_design.md`, `frontend_polish.md`, `data_visualization.md` — these are human-operator-facing (used when a developer works on the dashboard in a Claude Code session), not injected into any pipeline LLM call | n/a for this category | n/a |
| **Publishing/account agents** | `publishing_agent` (YouTube upload, no LLM call) | None — no LLM in this stage | n/a; project skill `publishing-preflight.md` is the human-operator equivalent, invoked before a run, not injected into the agent |
| **Runtime/ops agents** | `run_worker.py`, `scheduler.py`, `queue.py`, `factory_director.py`, `decision_engine.py` — none call an LLM directly for orchestration decisions (verify `decision_engine.py` / `evolution_engine.py` / `learning_engine.py` at implementation time — not exhaustively checked in this pass, flagged as an open item) | Project skills `diagnostics.md`, `collect.md`, `cost-forensic.md`, `budget-run.md` are the human-operator layer for this category | n/a |
| **No-skill agents** | All 35 agents listed in §3b | — | — |

---

## 5. Miswired / Overloaded / Missing Skills

**Skills loaded on wrong agents:** None found. All 5 currently-wired internal skills (§2 table) are loaded on agents whose stage genuinely needs that vocabulary — this matches the B0/B1 reports' own stated rationale and nothing has drifted since 2026-07-08.

**Agents missing required skill support:** None found as hard requirements. Two soft candidates are noted in §3a as open questions (Script Engine's "visual directions," reactivated Brand Guardian's brand-bible content) — neither is a confirmed gap, both require a product decision before any wiring.

**Redundant skills (structural, confirmed) — see Correction Note at top of document for resolution:**
- The 8 skills present in *both* `/Users/gawadeditor/.claude/skills/<name>/SKILL.md` (global, proper frontmatter format) and `.claude/skills/<name>.md` (project, flat format) — `bga-preflight`, `budget-run`, `collect`, `cost-forensic`, `diagnostics`, `brand-extract`, `visual-review`, `ep0-run`. This original finding was based on `wc -c` byte-count matching only. A follow-up sha256 verification pass (see Correction Note) found 6 were true byte-identical duplicates and have since been removed from the project location; 2 (`visual-review`, `ep0-run`) were false positives — their project copies contain genuinely different, newer content and remain in place pending a manual merge decision.

**Reconstructed skills that must later be replaced by upstream originals:** None identified as "reconstructed" in the sense of imitating a known upstream skill. The `graphify` skill (`.claude/skills/graphify/SKILL.md`) is itself upstream-installed (not reconstructed) and should not be hand-edited. No other skill in this repo claims to be a stand-in for an unavailable upstream original.

**Skills too large for runtime prompts:**
- `veo_translation_skill.md` (65,463 B) and `seedance_translation_skill.md` (57,020 B) full versions — already mitigated: agents load the `.compact.md` versions by default (7,285 B / 8,501 B), per B1. The full versions remain on disk for human reference only and are never injected. No action needed; flagged here only for completeness since the raw file sizes look alarming in isolation.
- `provider_skill_template.md` (17,974 B) and `veo_prompt_skill.md` (60,431 B) are both large but correctly never injected (governance / superseded respectively).

**Structural miswiring — a "skill" that is actually an agent:**
- `hypogeum/agents/thumbnail_strategy.py` calls `run_agent()` directly with its own model slug and writes a validated output package (`thumbnail_concept_brief`). By this repo's own architecture (skills = static reference markdown loaded via `compose_system_prompt()`; agents = modules that call `run_agent()`), this file is an agent, not a skill, and its location under `hypogeum/skills/` rather than `hypogeum/agents/` is inconsistent with every other LLM-calling module in the codebase. It is currently invoked manually via a separate script (`scripts/generate_thumbnail_strategy.py`, per its own docstring), not from `pipeline.py`. Recommend relocating to `hypogeum/agents/` when this Phase-2 wiring work happens (not now — audit only).

**Orphaned prompt file (found, not previously documented in A0/B0/B1):**
- `hypogeum/prompts/editorial_qa.txt` exists on disk but no code (`grep` across `hypogeum/`) references it — `hypogeum/agents/editorial_qa.py` does not call `load_prompt()`, `compose_system_prompt()`, or reference the file at all. This is a new finding not present in the three prior architecture reports read for this audit; it is the prompt-file equivalent of the skill-wiring gaps A0 found (M-02/m-01), just discovered one layer over from where B0 already looked.

---

## 6. External Skill Source Requirements

**Known upstream URLs (confirmed in this repo):**
- `graphify` — `https://github.com/safishamsi/graphify` (per `GRAPHIFY_VERIFICATION_REPORT.md` §1, "canonical upstream source, per explicit user confirmation"). This is a tool, not a skill per se, but its skill (`.claude/skills/graphify/SKILL.md`) is installed by it.

**Missing URLs — named in prior docs as planned installs, no source recorded anywhere in the repo:**
`SKILL_LOADING_AUDIT.md` §10 names 7 skills as preconditions for future installation: *Humanizer, Seedance2, GPT Image2, Veo Prompt Director, Veo Tools, UI/UX Pro Max, Graphify*. Graphify is now installed (URL known, above). The remaining six — **Humanizer, Seedance2, GPT Image2, Veo Prompt Director, Veo Tools, UI/UX Pro Max** — have no upstream URL recorded in `CLAUDE.md`, `DECISION_LOG.md`, `roadmap.md` (per memory), or any `docs/architecture/*.md` file read for this audit.

**BLOCKED — explicit:**
- Humanizer — source **BLOCKED**, no URL known.
- Seedance2 — source **BLOCKED**, no URL known. (Note: `hypogeum/humanizer.py` and Seedance-related code already exist in this repo as internal modules — do not conflate an existing internal module with an unwritten external "skill" of the same conceptual name. If a "Seedance2 skill" is wanted, it is a distinct artifact from `hypogeum/skills/seedance_translation_skill.md`, which already exists and is wired.)
- GPT Image2 — source **BLOCKED**, no URL known.
- Veo Prompt Director — source **BLOCKED**, no URL known. (Note: `veo_translation_skill.md` and the superseded `veo_prompt_skill.md` already exist internally; a "Veo Prompt Director" skill, if distinct, is unconfirmed.)
- Veo Tools — source **BLOCKED**, no URL known.
- UI/UX Pro Max — source **BLOCKED**, no URL known.

**Explicit instruction, restated per task requirement:** Do not recreate or imitate any of these six upstream skills from training-data memory or by guessing at typical content for a skill with that name. If they are wanted, the correct next step is obtaining the actual upstream source (repository URL, marketplace listing, or export) before any file is written under that name.

---

## 7. Recommendations

**Next smallest safe step after this audit:** Resolve the confirmed byte-identical duplication between the 8 global (`~/.claude/skills/<name>/SKILL.md`) and project (`.claude/skills/<name>.md`) skill pairs (§5). This is the lowest-risk, most mechanical finding in this report — no content decision required, just a canonical-location decision (global vs. project) — and it directly serves `PROJECT_PRINCIPLES.md` §7 ("token efficiency," "maintainability") without touching any agent, prompt, or pipeline code. It should still be scoped as its own follow-up task, not folded into this audit.

Other candidates, ordered by risk (do **not** implement any of these now):
1. Add YAML frontmatter to the 15 flat project skills (`SKILL_LOADING_AUDIT.md` M-01) — mechanical, low risk, improves discovery-hint precision.
2. Decide the Script Engine / reactivated Brand Guardian open questions in §3a as explicit product decisions before any skill is wired to either.
3. Relocate `hypogeum/agents/thumbnail_strategy.py` to `hypogeum/agents/` when its Phase 2 pipeline wiring work begins (its own docstring already anticipates this).
4. Decide the disposition of `hypogeum/prompts/editorial_qa.txt` (wire it into `editorial_qa.py`, or confirm it's intentionally dormant and document that, matching how `provider_skill_template.md` and `veo_prompt_skill.md` are explicitly documented as intentionally-unwired rather than silently orphaned).
5. Do not pursue the six BLOCKED external skills until real upstream sources are obtained.

No implementation is authorized by this document. Per `PROJECT_CONSTITUTION.md`, this document's findings are inputs to a future, separately-scoped decision — not a plan to execute.
