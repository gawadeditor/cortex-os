# A0 — Skill Loading Architecture Audit

**Date:** 2026-07-08
**Scope:** Read-only investigation of all skill discovery, loading, injection, caching, and transformation logic
**Status:** COMPLETE

---

## 1. Skill Discovery

### Where Skills Are Searched

Three distinct directories contain skills. Discovery is **static** — skills are not auto-scanned at runtime; each path is either hard-coded in Python imports or registered via the Claude Code plugin manifest system.

| Directory | Scope | Discovery Method |
|---|---|---|
| `/Users/gawadeditor/.claude/skills/` | Global (all projects) | Claude Code runtime reads at session start |
| `/Users/gawadeditor/hypogeum-brain/.claude/skills/` | Project-level | Claude Code runtime reads at session start |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/skills/` | Internal/Agent | Hard-coded `Path` constants in Python agent modules |

A fourth path — `.claude/plugins/marketplaces/claude-plugins-official/plugins/*/skills/*/SKILL.md` — exists for plugin-distributed skills but is not a Hypogeum-specific mechanism.

### Discovery Component

**For Claude Code skills (user-facing):** Discovery is performed by the Claude Code runtime itself. It reads SKILL.md files from both global and project-level `.claude/skills/` directories at session initialization and injects them into the system prompt as the "skills available for use with the Skill tool" list.

**For internal agent skills:** Discovery is performed at Python module import time via hard-coded `Path` constants in `hypogeum/agents/visual_package_director.py`. No scanning — specific file paths are referenced directly.

### Discovery: Static or Dynamic

**Static.** Adding a new skill file to disk does not automatically make it available. For user-facing skills, the Claude Code runtime must register them (which happens on session start). For agent skills, the agent source code must be modified to import the new path.

---

## 2. Runtime Flow

### Flow A — User Invokes a Claude Code Skill (`/ep0-run`, `/diagnostics`, etc.)

```
User types: /ep0-run
        ↓
Claude Code UI intercepts the /skill-name prefix
        ↓
Skill tool executed (invokes Skill tool internally)
        ↓
Claude Code runtime locates skill file:
  .claude/skills/ep0-run.md  (project-level, no frontmatter)
  OR
  .claude/skills/<dir>/SKILL.md  (user-level, YAML frontmatter)
        ↓
Skill file content read from disk (no cache, re-read each invocation)
        ↓
Content injected into current conversation context
        ↓
Claude executes skill instructions within the conversation turn
        ↓
No persistent state — skill content not retained after conversation ends
```

### Flow B — Python Pipeline Agent Loads Internal Skill

```
Python process starts → agent module imports
        ↓
Module-level constants evaluated (visual_package_director.py):
  _CINEMATIC_SKILL_FILE = Path(...) / "cinematic_prompt_architecture.md"
  _VLE_SKILL_FILE       = Path(...) / "visual_language_engine.md"
        ↓
prompt_loader.compose_system_prompt(base_prompt, skill1, skill2)
  → load_prompt(base_prompt_path)    # strips metadata headers, returns body
  → load_skill(cinematic_path)       # reads verbatim, returns str
  → load_skill(vle_path)             # reads verbatim, returns str
  → join with "\n\n---\n\n"
        ↓
Result stored in module-level constant:
  SCENE_PROMPTS_PROMPT: str = <composed string>   ← FROZEN IN MEMORY
        ↓
Pipeline run begins → agent.run_agent() called
        ↓
SCENE_PROMPTS_PROMPT passed as system message to OpenRouter Claude API
        ↓
Claude sees: [base prompt] --- [cinematic skill] --- [VLE skill]
Claude generates scene prompts using skill guidelines
        ↓
Output validated by schemas.py (schema validation is independent of skill)
        ↓
Module-level constant never refreshed within same Python process
```

### Flow C — System Prompt Injection (Session Start)

```
Claude Code session starts
        ↓
Runtime scans:
  /Users/gawadeditor/.claude/skills/*/SKILL.md           (27 global skills)
  /Users/gawadeditor/hypogeum-brain/.claude/skills/*.md  (15 project skills)
        ↓
Frontmatter parsed (YAML) for SKILL.md files:
  name, description, metadata.tags extracted
Project skills (plain markdown, no frontmatter):
  Filename used as identifier; first heading used as description
        ↓
Assembled into system reminder block:
  "The following skills are available for use with the Skill tool: ..."
        ↓
Block injected once at session start — not refreshed during session
        ↓
Claude can invoke any listed skill via the Skill tool
```

---

## 3. Source Code Trace

All skill-related logic in the Hypogeum Python codebase is confined to **one module**:

### `hypogeum/prompt_loader.py`

| Function | Responsibility |
|---|---|
| `load_skill(path: Path) → str` | Reads skill markdown file verbatim. Returns `""` if file missing (graceful degradation). No header stripping. |
| `load_prompt(path: Path) → str` | Reads prompt file. Strips `# KEY: VALUE` metadata headers before returning body. Used by agents for base prompts only, never for skills. |
| `compose_system_prompt(base_prompt_path, *skill_paths) → str` | Calls `load_prompt(base)` then `load_skill(s)` for each skill path. Joins all with `"\n\n---\n\n"`. Returns base prompt even if all skill files are missing. |

### `hypogeum/agents/visual_package_director.py`

| Lines | Responsibility |
|---|---|
| Module-level constants | Hard-coded `Path` references to skill files. |
| `_load_scene_prompts_prompt()` | Calls `compose_system_prompt()` with base prompt + cinematic + VLE skill. Result cached in `SCENE_PROMPTS_PROMPT`. |
| Agent call sites | Pass `SCENE_PROMPTS_PROMPT` as system message to `run_agent()`. |

### All Other Agents

No other agent in `hypogeum/agents/` loads any skill file. Verified by searching for `load_skill` and `compose_system_prompt` references — only `visual_package_director.py` uses them.

---

## 4. Markdown Handling

What happens after a skill Markdown file is found:

| Step | User-facing Skills (`.claude/skills/`) | Internal Agent Skills (`hypogeum/skills/`) |
|---|---|---|
| **Read** | Yes — full content read from disk on each invocation | Yes — read once at Python module import |
| **Convert** | No — markdown is not converted | No |
| **Summarize** | No | No |
| **Inject into prompts** | Yes — injected into conversation context as skill content | Yes — appended to agent system prompt via `compose_system_prompt()` |
| **Merge into CONTEXT** | No | No |
| **Cache** | No — re-read on each `/skill-name` call | Yes — frozen in module-level `str` constant |
| **Reload every run** | Yes (per invocation) | No (once per Python process lifetime) |
| **Header stripping** | Not applicable (no metadata headers) | `load_skill()` does NOT strip headers; `load_prompt()` does. Skills are passed verbatim. |

**Evidence:** `prompt_loader.py` `load_skill()` reads the file path and returns its content unchanged. There is no regex, YAML parser, or transformer applied to skill content.

---

## 5. Prompt Injection

Skill contents are copied into:

**For user-facing skills:**
- Injected into **conversation context** (current turn) when invoked via the `Skill` tool or `/skill-name` prefix
- Not injected into persistent system prompt; content is per-invocation only

**For internal agent skills:**
- Injected into the **agent system prompt** via `compose_system_prompt()` output stored in `SCENE_PROMPTS_PROMPT`
- This constant is passed as the `system=` argument to every `run_agent()` call for that agent
- Skill content becomes part of every OpenRouter API call that agent makes

**Not injected into:**
- `CONTEXT.md` — no such file exists; skills do not write to CLAUDE.md
- Runtime prompt (user message) — skills are always in system position, not user position
- Database — no skill content is persisted to SQLite

---

## 6. Wrapper Detection

### Hypogeum's Custom Layer Over Standard Skill Workflow

Hypogeum introduces **one custom module** that wraps standard file I/O into a composed system prompt builder:

**`hypogeum/prompt_loader.py`** — the only wrapper

- **Type:** Prompt composer / loader
- **Function:** Reads base prompts + skill files, joins them with a separator, returns a single string
- **Not a:** Parser, transformer, serializer, or adapter — it performs no content modification
- **Evidence:** `compose_system_prompt` is `"\n\n---\n\n".join(parts)` where each part is raw file content

No other wrapper, adapter, parser, or transformer exists. There is no:
- YAML frontmatter parser in Hypogeum Python code (frontmatter parsing is Claude Code runtime's job)
- Skill registry or manifest within the Python codebase
- Dynamic skill loader or skill discovery engine
- Caching layer beyond Python module-level constants

---

## 7. Compatibility Audit

### Expected Standard Skill Workflow

```
GitHub Repository
        ↓
Clone → .claude/skills/<name>/SKILL.md
        ↓
Claude Code runtime reads at session start
        ↓
SKILL.md content injected into conversation when invoked
        ↓
Claude reads it directly — no transformation
```

### Current Implementation vs Expected

| Feature | Expected | Current | Match |
|---|---|---|---|
| Skill directory location | `.claude/skills/` | `.claude/skills/` (both global + project) | ✓ MATCH |
| Skill file format (user-facing) | `SKILL.md` with YAML frontmatter in directory | SKILL.md (global) + plain `.md` (project) | PARTIAL — project skills lack frontmatter |
| Discovery trigger | Session start (automatic) | Session start (automatic) | ✓ MATCH |
| Runtime injection | Into conversation context | Into conversation context | ✓ MATCH |
| Content transformation | None — Claude reads markdown directly | None — verbatim pass-through | ✓ MATCH |
| Caching | Not specified in standard | User-facing: per-call; Agent: module-level | ✓ ACCEPTABLE |
| Invocation mechanism | `/skill-name` or `Skill` tool | `/skill-name` or `Skill` tool | ✓ MATCH |
| Code execution in skills | No — markdown only | No — markdown only | ✓ MATCH |
| Internal agent skills | Not part of standard workflow | `hypogeum/skills/` loaded via `prompt_loader.py` | N/A — extension, not conflict |
| Plugin-distributed skills | Not in standard workflow | `.claude/plugins/` supported | N/A — extension |

### Compatibility Assessment

**The user-facing skill layer is FULLY COMPATIBLE with the standard GitHub Skill workflow.**

The only deviation is that project-level skills (`.claude/skills/*.md`) use plain markdown without YAML frontmatter. The standard expects a `SKILL.md` file inside a named directory with frontmatter. The project-level format works because Claude Code falls back to filename-based identification when frontmatter is absent, but descriptions shown in the system reminder may be less precise.

**The internal agent skill layer** (`hypogeum/skills/` + `prompt_loader.py`) is an **addendum** — a separate mechanism for injecting reference content into LLM API calls made by Python code. It does not conflict with the standard Claude Code skill workflow; it operates at a completely different layer.

---

## 8. Architecture Assessment

**The current implementation is: FULLY COMPATIBLE**

Rationale:

1. The user-facing skill layer (`.claude/skills/`) follows the standard workflow exactly: SKILL.md files with frontmatter, discovered at session start, injected on invocation, no transformation.

2. The project-level procedure skills (`.claude/skills/*.md`) are a minor format deviation (no frontmatter directory structure) but are functionally compatible — Claude Code registers and invokes them correctly.

3. The internal agent skill layer (`hypogeum/skills/` via `prompt_loader.py`) is an **independent extension** that does not interfere with the standard workflow. It serves a different purpose: embedding reference content into Python-orchestrated LLM calls.

4. No anti-patterns: no duplicate loaders, no conflicting parsers, no skill content modification, no circular dependencies between the two skill layers.

---

## 9. Findings

### Critical

None.

### Major

**M-01: Project-level skills lack YAML frontmatter**
- **File:** `/Users/gawadeditor/hypogeum-brain/.claude/skills/*.md` (15 files)
- **Issue:** Standard SKILL.md format requires YAML frontmatter (`name`, `description`, `metadata.tags`) inside a named directory. Project skills are flat `.md` files with no frontmatter.
- **Effect:** Skill descriptions shown in system reminder are derived from filename or first heading, not a curated description field. May cause imprecise matching or discovery.
- **Risk:** Low for existing skills (all work). Higher for new skills where precise `description` field matters for invocation hint quality.

**M-02: Internal agent skills not visible to new agents**
- **File:** `hypogeum/skills/seedance_translation_skill.md`, `veo_translation_skill.md`, `veo_prompt_skill.md`
- **Issue:** Three provider translation skills exist in `hypogeum/skills/` but are not loaded by any agent. They are reference documents with no import path. `prompt_loader.py` is never called for them.
- **Effect:** Skills authored but not injected. Any agent that would benefit from Seedance or Veo translation guidance is not receiving it.
- **Risk:** Medium. New video generation skills (Humanizer, Seedance2, Veo Prompt Director, Veo Tools) will face the same issue unless explicitly wired to `compose_system_prompt()`.

### Minor

**m-01: `thumbnail_intelligence_pipeline.md` not loaded by any agent**
- **File:** `hypogeum/skills/visual_intelligence/thumbnail_intelligence_pipeline.md`
- **Issue:** File exists in `visual_intelligence/` skill directory but no `load_skill()` call references it. The Thumbnail Director agent does not use it.
- **Effect:** Thumbnail intelligence pipeline guidelines are authored but not injected into the Thumbnail Director's system prompt.

**m-02: `shot_diversity_planner.py` loads no skills**
- **File:** `hypogeum/agents/shot_diversity_planner.py`
- **Issue:** This agent plans shot diversity but does not load `visual_language_engine.md` or `cinematic_prompt_architecture.md`, which directly govern shot scale and camera movement vocabulary.
- **Effect:** Shot diversity planning operates without the cinematic constraint layer.

**m-03: Module-level constant pattern prevents hot-reload**
- **Files:** `hypogeum/agents/visual_package_director.py`
- **Issue:** Skills are loaded at module import time and frozen. If a skill file is edited during a running Python process, the change is not picked up until process restart.
- **Effect:** Development friction only — no production risk.

### Informational

**i-01: Two parallel skill systems with no bridge**

The Claude Code skill system (user-facing) and the `prompt_loader.py` system (agent-internal) are completely independent. There is no mechanism to install a `.claude/skills/` skill into the agent skill pipeline, or vice versa. This is a design choice, not a defect — but it means skills installed for human operators are never available to the pipeline LLMs.

**i-02: `load_skill()` silently skips missing files**

`prompt_loader.py` `load_skill()` returns `""` for missing paths with no log or warning. A misconfigured skill path produces a silently incomplete system prompt. There is no validation that expected skills are present.

**i-03: Skill content is never hashed or version-tracked**

No mechanism exists to detect when a skill file changes between production runs. There is no version field in skill frontmatter, no hash recorded in the database, no audit trail of which skill version was in effect for a given episode.

---

## 10. Final Verdict

**PASS**

The Skill loading architecture is fully compatible with the standard GitHub Skill workflow. The standard pipeline (GitHub repo → clone → `.claude/skills/` → SKILL.md → Claude Code reads at session start → injected on invocation) works as expected. No architectural blocker exists for installing additional skills.

**Preconditions for installing new skills (Humanizer, Seedance2, GPT Image2, Veo Prompt Director, Veo Tools, UI/UX Pro Max, Graphify):**

1. **Format:** New user-facing skills must follow the standard structure: a named directory inside `.claude/skills/` containing a `SKILL.md` file with YAML frontmatter (`name`, `description`, `metadata.tags`). Do not use the flat `.md` format of the current project-level skills.

2. **Agent wiring (if applicable):** If a new skill must be active inside a Python pipeline agent (e.g., Veo Prompt Director, Seedance2 translation), it must be:
   - Placed in `hypogeum/skills/`
   - A `Path` constant added to the relevant agent module
   - Passed to `compose_system_prompt()` alongside existing skills

3. **No action needed** for the two-system design — Claude Code skill layer and agent skill layer are independent by design and compatible.

4. **Address M-02** before adding Veo Prompt Director or Seedance2 as agent skills: verify that `compose_system_prompt()` is called with the new skill path in every agent that needs it. Silent miss (finding M-02) could leave a new skill installed but never injected.

---

## Appendix: Files Examined

| File | Purpose |
|---|---|
| `/Users/gawadeditor/.claude/settings.json` | Global hooks configuration |
| `/Users/gawadeditor/.claude/settings.local.json` | Global tool permissions |
| `/Users/gawadeditor/.claude/skills/01-cinematic/SKILL.md` | Sample user-level SKILL.md (frontmatter format) |
| `/Users/gawadeditor/.claude/skills/remotion/SKILL.md` | Sample user-level SKILL.md (with rules/ references) |
| `/Users/gawadeditor/.claude/skills/harness/SKILL.md` | Sample user-level SKILL.md |
| `/Users/gawadeditor/hypogeum-brain/.claude/settings.json` | Project hooks configuration |
| `/Users/gawadeditor/hypogeum-brain/.claude/settings.local.json` | Project tool permissions |
| `/Users/gawadeditor/hypogeum-brain/.claude/skills/ep0-run.md` | Sample project-level skill (plain markdown) |
| `/Users/gawadeditor/hypogeum-brain/.claude/skills/collect.md` | Sample project-level skill |
| All 15 files in `.claude/skills/` | Project-level skill inventory |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/prompt_loader.py` | Only Python module that reads skill files (183 lines) |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/agents/visual_package_director.py` | Only agent that calls `compose_system_prompt()` |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/agents/shot_diversity_planner.py` | Agent examined for skill usage (none found) |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md` | Agent skill loaded by VPD |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/skills/visual_intelligence/visual_language_engine.md` | Agent skill loaded by VPD |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/skills/seedance_translation_skill.md` | Provider reference skill (not loaded by any agent) |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/skills/veo_translation_skill.md` | Provider reference skill (not loaded by any agent) |
| `/Users/gawadeditor/hypogeum-brain/hypogeum/skills/veo_prompt_skill.md` | Provider reference skill (not loaded by any agent) |
| `/Users/gawadeditor/.claude/plugins/marketplaces/claude-plugins-official/plugins/*/SKILL.md` | Plugin-distributed skill format reference |
| `/Users/gawadeditor/hypogeum-brain/CLAUDE.md` | Project documentation |
