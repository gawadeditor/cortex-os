# A1 — Graphify Integration Design

**Date:** 2026-07-09
**Status:** READ-ONLY DESIGN — no installation, no code changes
**Source:** https://github.com/Graphify-Labs/graphify (read 2026-07-09)
**Depends on:** docs/architecture/ARCHITECTURE_AUDIT.md (A0)

---

## 1. What Graphify Is (According to the Repo)

Graphify is a CLI tool and Claude Code skill (PyPI package: `graphifyy`) that transforms code repositories, documentation, and media files into a **queryable knowledge graph**. It is not a vector embedding system and not a RAG index — it produces an explicit graph with traversable nodes and typed edges.

### 1.1 Core Mechanism

**Code files** are parsed using tree-sitter AST grammars (36+ languages). This is deterministic, requires zero LLM calls, and nothing leaves the machine. The output is a set of nodes (functions, classes, modules, symbols) and edges (imports, calls, uses) with explicit source locations.

**Document files** (Markdown, HTML, RST, YAML, PDF, Office) are processed by semantic subagents dispatched in parallel via the Agent tool. Wikilinks and cross-document references become graph edges.

**Media files** (images, video, audio) can be transcribed and indexed via optional extras (`[pdf]`, `[video]`).

Every edge is tagged with a confidence label:

| Label | Confidence | Meaning |
|---|---|---|
| `EXTRACTED` | 1.0 | Explicitly present in source (import statement, direct function call) |
| `INFERRED` | 0.4–0.9 | Reasonable deduction (call-graph second pass, co-occurrence) |
| `AMBIGUOUS` | 0.1–0.3 | Uncertain; flagged for human review in GRAPH_REPORT.md |

### 1.2 What It Is Not

Per the repo:
- Not a prompt layer. It produces no prompts and runs no model during code extraction.
- Not provider-specific. It has no awareness of Veo, Seedance, MUAPI, or OpenRouter.
- Not an agent. It does not call any pipeline agent or produce any Hypogeum artifact.
- Not a skill document in the `hypogeum/skills/` sense. It is a separate installed tool that registers as a Claude Code skill via `graphify install`.
- Not a substitute for reading source files when editing. The graph is a navigation aid, not a replacement for verified source truth.

### 1.3 Processing Pipeline (9 Steps, per repo)

```
1. Installation check      — verify graphify is available
2. File detection          — scan corpus; warn if >2M words or >200 files
3. Extraction              — parallel AST (code) + semantic subagents (docs/images)
4. Graph building          — construct NetworkX graph, run Leiden community detection
5. Community labeling      — assign 2–5 word human-readable names to clusters
6. Obsidian + HTML export  — generate vault and interactive visualization
7. Neo4j export            — conditional on --neo4j flag
8. Token reduction benchmark — if corpus >5,000 words
9. Manifest + reporting    — GRAPH_REPORT.md, cleanup
```

Internal architecture (from ARCHITECTURE.md):
```
detect() → extract() → build_graph() → cluster() → analyze() → report() → export()
```

Each stage is a single function in its own module, communicating through plain Python dicts and NetworkX graphs. No shared state; no side effects outside `graphify-out/`.

---

## 2. How It Is Intended to Be Installed/Run

From PyPI page and README (verified 2026-07-09):

### 2.1 Installation

```bash
uv tool install graphifyy        # recommended — isolated environment
graphify install                 # registers Graphify with Claude Code (and other assistants)
```

Alternative installers:
```bash
pipx install graphifyy           # alternative isolated install
pip install graphifyy            # direct install (not recommended — pollutes global env)
```

Optional capability extras:
```bash
uv tool install "graphifyy[pdf]"     # PDF extraction
uv tool install "graphifyy[video]"   # video/audio transcription
uv tool install "graphifyy[neo4j]"   # Neo4j Cypher export
uv tool install "graphifyy[office]"  # .docx, .xlsx support
```

> **Note:** The PyPI package name is `graphifyy` (two y's). The CLI command is `graphify` (one y). This asymmetry is documented on the PyPI page and is intentional.

### 2.2 Running

After installation, invoke from Claude Code:

```
/graphify .                        # build graph for current directory
/graphify . --mode deep            # thorough semantic inference
/graphify . --update               # incremental re-extraction (changed files only)
/graphify . --watch                # auto-rebuild on file changes
```

Query the built graph:
```
/graphify query "what connects auth to the database?"
/graphify path "pipeline.py" "muapi.py"
/graphify explain "run_agent"
```

Auto-rebuild hook:
```bash
graphify hook install              # auto-rebuild on git commit
```

---

## 3. What Outputs It Creates

All outputs are written to `graphify-out/` in the directory where Graphify was invoked.

```
graphify-out/
├── GRAPH_REPORT.md          — plain-language highlights: god nodes, surprising connections,
│                              community names, suggested questions for exploration
├── graph.json               — full persistent graph (nodes + edges + metadata)
│                              queryable without re-reading source files
├── graph.html               — interactive force-directed visualization with clickable
│                              nodes and community filtering
├── obsidian/
│   ├── graph.canvas         — Obsidian canvas file
│   └── [community notes]    — one Markdown note per detected community
└── [optional exports]
    ├── graph.svg            — static SVG export
    ├── graph.graphml        — GraphML for external tools
    └── cypher.txt           — Neo4j Cypher statements (--neo4j flag)
```

**`graph.json`** is the key persistent artifact. It survives across Claude Code sessions. Once built, the graph can be queried without re-reading any source files, enabling sub-second lookups instead of multi-file sequential reads.

**`GRAPH_REPORT.md`** identifies:
- "God nodes" — high-degree nodes (things everything depends on)
- Surprising connections — edges between distant communities
- Suggested questions — graph-derived prompts for exploration

**Performance claim from repo:** "71.5x fewer tokens per query vs reading raw files."

---

## 4. How It Could Apply to the Hypogeum Project

The Hypogeum repo has a known navigation problem (documented in A0): 48 agent modules, 20 prompt files, 11 skill files, a 1,800-line pipeline orchestrator, 31 schemas, and 4 external providers — all cross-referencing each other with no machine-readable interface declarations. Every open-ended question (e.g., "which agents call run_agent()?", "what does VPD pass to TD?", "what is the token budget chain?") currently requires sequential file reads across multiple modules.

Graphify addresses this directly:

| Current approach | With Graphify |
|---|---|
| Read pipeline.py (1,800 lines) to find agent order | `graphify query "pipeline execution order"` |
| Grep for all `compose_system_prompt` call sites | `graphify query "which prompts compose skills"` |
| Read 3+ agent files to trace a data field | `graphify path "script_package" "muapi.py"` |
| Manually reconstruct dependency graph for audit | `graph.html` shows it visually |
| Find all agents that consume `visual_package` | `graphify explain "visual_package"` |
| Identify god nodes (everything depends on) | `GRAPH_REPORT.md` highlights them automatically |

**Expected god nodes for Hypogeum** (predicted from A0 audit):
- `pipeline.py` — calls all 9 core agents
- `agent.py` — called by every LLM-based agent
- `config.py` — consumed by every agent module
- `schemas.py` — validates every agent output
- `run_agent()` — single LLM call entrypoint
- `muapi.py` — all MUAPI video/image calls

**Expected communities** (predicted from A0):
- VPD cluster: visual_package_director, shot_diversity_planner, scene_planner, entity_continuity_manager, provider_router, video_generation_agent, image_generation_agent
- Thumbnail cluster: thumbnail_director, thumbnail_prompt_writer, thumbnail_composition_planner, thumbnail_typography_planner, thumbnail_generation_agent, thumbnail_identity_guardian, thumbnail_qa
- Audio cluster: audio_agent, voice_generation_operator, caption_agent, audio_qa
- Assembly cluster: assembly_agent, timeline_builder, timeline_qa, music_agent, video_renderer, final_render_reviewer
- Governance cluster: qa_agent, compliance_agent, archive_agent, finance_agent, brand_guardian_agent
- Infrastructure cluster: pipeline.py, agent.py, config.py, schemas.py, muapi.py, openrouter.py, prompt_loader.py, skill_registry.py

---

## 5. Which Folders Should Be Indexed

### 5.1 Include

| Path | File Types | Rationale |
|---|---|---|
| `hypogeum/agents/` | Python (48 .py) | Core agent modules — the primary code graph |
| `hypogeum/prompts/` | .txt (20 files) | Prompt files — document nodes linked to agent loaders |
| `hypogeum/skills/` | Python + .md | Skill registry (.py) and skill documents (.md) |
| `hypogeum/skills/visual_intelligence/` | .md | Visual intelligence skills |
| `hypogeum/skills/visual_package_director/` | .md | CPA skill |
| `hypogeum/brand/` | .md | Brand spec documents |
| `hypogeum/pipeline.py` | Python | Orchestrator — highest-degree node expected |
| `hypogeum/agent.py` | Python | Central LLM runner |
| `hypogeum/config.py` | Python | Configuration hub |
| `hypogeum/schemas.py` | Python | 31 schema validators |
| `hypogeum/muapi.py` | Python | MUAPI client |
| `hypogeum/openrouter.py` | Python | LLM client |
| `hypogeum/prompt_loader.py` | Python | Prompt loading utilities |
| `hypogeum/skill_registry.py` | Python | Skill registry |
| `hypogeum/db.py` | Python | Database access layer |
| `hypogeum/logger.py` | Python | Logging infrastructure |
| `hypogeum/finance.py` | Python | Cost tracking |
| `hypogeum/topic_discovery.py` | Python | Discovery engine |
| `hypogeum/discovery/` | Python | Discovery sub-modules |
| `tests/` | Python | Test suite — reveals expected contracts |
| `docs/architecture/` | .md | Architecture documents (including A0 audit) |

### 5.2 Conditionally Include (assess after first build)

| Path | Reason for caution |
|---|---|
| `hypogeum/skills/compiled/` | Derived compact files — not canonical source; graph may confuse full vs compact versions |
| `run_dashboard.py` | 5,600+ lines; may inflate the graph disproportionately; index separately if needed |
| `run_pipeline.py` | Single-purpose entry point; useful for wiring context but small signal |
| `run_worker.py` | Worker loop; useful context for autonomous operation |
| `scripts/` | Ad-hoc maintenance scripts; low signal for architectural understanding |

---

## 6. Which Folders Should Be Excluded

These must be listed in `.graphifyignore` (gitignore syntax). `.gitignore` is automatically respected by Graphify, so any path already in `.gitignore` does not need to be repeated.

### 6.1 Must Exclude (correctness and safety)

| Path | Reason |
|---|---|
| `.hypogeum-assets/` | Generated episode outputs — videos, audio, thumbnails, JSON checkpoints, pipeline logs. Large binary files. Not source. |
| `.env`, `.env.*`, `.env.backup.*` | Credentials. Must never be indexed. |
| `graphify-out/` | Graphify's own output directory — circular ingestion would corrupt the graph. |
| `__pycache__/`, `*.pyc`, `*.pyo` | Compiled bytecode — redundant with source; adds noise. |
| `.pytest_cache/` | Test runner cache — not source. |
| `*.log` | Pipeline log files — high volume JSONL, not structural source. |
| `hypogeum.db`, `*.db`, `*.sqlite` | SQLite databases — binary; not parseable as source. |

### 6.2 Exclude (noise reduction)

| Path | Reason |
|---|---|
| `*.png`, `*.jpg`, `*.jpeg` | Screenshot files at repo root (p12-*.png, check*.png, etc.) — not codebase documentation. Exclude unless `[pdf]` extra and vision indexing are specifically desired. |
| `brand_board.jpg`, `tiktok_icon_1024.jpg` | Binary image assets — not structural source. |
| `docs/brand_guardian/*.png` | Reference images — binary. |
| `.playwright-mcp/` | Playwright tooling cache. |
| `experiments/` | If present: experimental code not in production path. |
| `tools/` | Ad-hoc one-off scripts. Assess per-item. |
| `venv/`, `.venv/` | Python virtual environment — if present. |
| `node_modules/` | If present. |

### 6.3 Proposed `.graphifyignore` Content

```
# Generated episode outputs — videos, audio, thumbnails, checkpoints
.hypogeum-assets/

# Graphify's own output (prevent circular ingestion)
graphify-out/

# Credentials
.env
.env.*
.env.backup*

# Python artifacts
__pycache__/
*.pyc
*.pyo
.pytest_cache/

# Logs and databases
*.log
*.db
*.sqlite

# Screenshot and binary image noise at repo root
*.png
*.jpg
*.jpeg
brand_board.jpg
tiktok_icon_1024.jpg

# Platform-specific screenshots
p12-*.png
check*.png
tiktok-*.png
meta-*.png

# Ad-hoc tooling
.playwright-mcp/

# Large entry points (index separately if desired)
run_dashboard.py
```

> **Note:** `.gitignore` is automatically respected by Graphify. Only paths NOT already in `.gitignore` need to appear in `.graphifyignore`.

---

## 7. Where `graphify-out/` Should Live

**Location:** `hypogeum-brain/graphify-out/` (repo root).

**Rationale:**
- Graphify should be invoked from the repo root (`/graphify .`) so the graph covers the entire indexed codebase in one pass.
- Placing `graphify-out/` at the root keeps the output co-located with what it describes.
- The directory is a **derived artifact** — it must be added to `.gitignore` and never committed. It should be rebuilt from source on demand, not version-controlled.
- Running Graphify from a subdirectory (e.g., `hypogeum/`) would produce a partial graph missing the pipeline orchestrator, tests, and architecture docs. That is not the intended use.

**`.gitignore` entry required:**
```
graphify-out/
```

---

## 8. How Claude Code Should Use Graphify Before Expensive Repo Search/Read Operations

### 8.1 Trigger Conditions

Claude Code should reach for Graphify **before** performing any of the following:

| Expensive operation | Graphify alternative |
|---|---|
| Grepping across multiple files for a symbol | `graphify query "where is <symbol> defined and called"` |
| Reading 3+ agent files to answer "what calls what" | `graphify path "<source_node>" "<target_node>"` |
| Spawning an Explore subagent for open-ended codebase questions | `graphify query "<question>"` |
| Manually reconstructing a dependency chain for audit or planning | Read `graphify-out/graph.html` or `GRAPH_REPORT.md` |
| Tracing a data field across multiple agents | `graphify explain "<field_name>"` |

**Decision rule:** If the question can be answered by graph traversal (topology, relationships, dependencies), use Graphify. If the question requires verified current source content (for editing, schema validation, or reading exact logic), read the file directly.

### 8.2 Correct Usage Pattern

```
1. Check graphify-out/graph.json exists and is recent (not stale)
2. If graph exists:
     graphify query "<question>"          ← prefer this for topology questions
     graphify path "<node_a>" "<node_b>"  ← for dependency chains
     graphify explain "<concept>"         ← for concept detail
3. If graph is stale or absent:
     /graphify . --update                 ← incremental re-extraction on changed files
     /graphify .                          ← full rebuild if structure has changed
4. Use graph output to narrow the search, then Read specific files for exact content
```

### 8.3 What the Graph Answers Well

- "Which agents call `run_agent()`?"
- "What is the dependency chain from `pipeline.py` to `muapi.py`?"
- "What modules does `visual_package_director.py` import?"
- "Which files reference `SYSTEM_PROMPT`?"
- "What are the god nodes in this codebase?"
- "Which communities does the graph identify? What is in each?"
- "Are there any unexpected connections between the thumbnail and audio clusters?"

### 8.4 What the Graph Does NOT Answer

- "What is the current value of `MUAPI_T2_MODEL`?" → Read `config.py`
- "Does this function exist today?" → Read the file; graph may be stale
- "What does `validate()` do exactly?" → Read `schemas.py`
- "What is the exact prompt text in `thumbnail_director.txt`?" → Read the file
- "Is this the correct schema structure before I write a fix?" → Read `schemas.py`

### 8.5 Graph Staleness Check

Before trusting query results, verify graph currency:

```bash
# Check modification time of graph.json vs key source files
ls -la graphify-out/graph.json
git diff --name-only HEAD~5 | grep -E '\.py$|\.txt$|\.md$'
```

If significant structural files (agent modules, pipeline.py, schemas.py) have changed since the last build, run `--update` before querying.

---

## 9. What Graphify Must Never Replace

The following are authoritative sources that Graphify cannot substitute for, regardless of graph quality:

| Authority | Why Graphify cannot replace it |
|---|---|
| **`hypogeum/schemas.py`** | Ground truth for all 31 schema contracts. Editing any agent output requires reading the schema directly — graph nodes for schemas are structural summaries, not validators. |
| **`hypogeum/pipeline.py`** (for edit work) | The frozen pipeline execution order must be read and understood directly before any modification. The graph shows dependencies; it does not show execution semantics. |
| **`CLAUDE.md`** | Project instructions that override all default behavior. Graphify does not index or interpret CLAUDE.md as executable instructions. |
| **Provider API docs** | MUAPI, OpenRouter, ElevenLabs, Perplexity API contracts must be read from authoritative sources. The graph may show which Hypogeum modules call these providers, but not the provider's own contract. |
| **Prompt files** (when editing) | Prompts must be read directly before modification. The graph shows which agent loads which prompt; it does not show current prompt content. |
| **Skill files** (when editing) | Same as prompts — the graph shows wiring; the file contains the rules. |
| **`.env` and `config.py`** (for runtime values) | Config values in effect for a given run must be read from the source. The graph shows config is consumed; it does not show current values. |
| **Test files** (for correctness) | Test assertions are the behavioral contract for each module. Graphify shows which tests exist; it does not run them or interpret assertions. |
| **`graphify-out/` itself** | The graph is a derived artifact and is never the source of truth. If graph content conflicts with what a source file says, trust the source file and rebuild the graph. |

---

## 10. Risks

### R-01 — Graph Staleness (HIGH probability, MEDIUM impact)

**Description:** `graph.json` is built from a snapshot of the repo. After code changes — new agents, prompt modifications, schema additions, skill wiring changes — the graph will not automatically update unless `--update` is run or `graphify hook install` is active.

**Specific risk for Hypogeum:** The pipeline is under active development (B0/B1/A0/A1 in quick succession). A stale graph showing the old VPD skill paths would mislead queries about skill wiring.

**Mitigation:** Install the git hook (`graphify hook install`) so the graph rebuilds on every commit. Document the staleness check procedure (section 8.5). Always rebuild before a major audit or design session.

---

### R-02 — INFERRED Edge False Positives (MEDIUM probability, LOW-MEDIUM impact)

**Description:** The graph engine infers relationships from co-occurrence and call-graph second passes. In Hypogeum, many agent modules share imports (`from hypogeum.agent import run_agent`, `from hypogeum.config import Config`) that are not semantic dependencies — they are shared infrastructure. The graph may draw INFERRED edges between agents that have no direct runtime relationship.

**Example:** An INFERRED edge from `music_agent.py` to `visual_package_director.py` because both import `run_agent()` does not mean they interact at runtime.

**Mitigation:** Trust only `EXTRACTED` edges for architectural decisions. Treat `INFERRED` and `AMBIGUOUS` edges as hypotheses to verify against source. GRAPH_REPORT.md flags AMBIGUOUS edges explicitly.

---

### R-03 — File Count at Corpus Boundary (LOW probability, LOW impact)

**Description:** Graphify warns (and may ask for confirmation) if the corpus exceeds 200 files. The Hypogeum repo has 48 agent modules + 20 prompts + tests + docs but is unlikely to exceed 200 files in the indexed set. However, if `run_dashboard.py` (5,600 lines) is included, a single file may dominate the graph as an extreme god node, distorting community detection.

**Mitigation:** Index `run_dashboard.py` separately if its community clustering dominates the graph. The `.graphifyignore` entry for it is already included in section 6 as a conditional exclusion.

---

### R-04 — Module-Level Constant Extraction Limitation (MEDIUM probability, LOW impact)

**Description:** Graphify's AST extraction captures defined symbols and their call relationships. It may not fully capture **module-level constants loaded at import time** — specifically `SYSTEM_PROMPT`, `META_PROMPT`, `SCENE_PROMPTS_PROMPT` in VPD, and similar constants in other agents. These are computed by calling `compose_system_prompt()` at module import, which involves file I/O that static AST analysis cannot execute.

**Impact:** Queries like "what skills does VPD Stage B load?" may return incomplete results from the graph — they show the `compose_system_prompt` call but not the resolved skill file paths. The graph is correct about structure; the resolution of constants requires reading the source.

**Mitigation:** For skill-wiring questions, read the relevant agent module directly after using the graph to locate it. The graph navigates to the right module; the module contains the ground truth.

---

### R-05 — `.hypogeum-assets/` Accidental Inclusion (HIGH severity if it occurs)

**Description:** If `.hypogeum-assets/` is not properly excluded, Graphify would attempt to process large binary video files, audio files, and JSONL pipeline logs. This would likely exhaust time/memory and produce meaningless graph nodes.

**Mitigation:** `.graphifyignore` must include `.hypogeum-assets/` as the first entry. Verify exclusion before first run: `graphify detect . --list-files | grep hypogeum-assets` should return empty. (Command form is UNKNOWN — verify against installed version.)

---

### R-06 — `.env` Credential Exposure in Graph (HIGH severity, LOW probability)

**Description:** If `.env` files are not excluded and Graphify indexes them, API keys (OPENROUTER_API_KEY, ELEVENLABS_API_KEY, MUAPI_API_KEY, etc.) could appear as graph node content in `graph.json` or `graph.html`.

**Mitigation:** `.graphifyignore` must explicitly exclude `.env`, `.env.*`, `.env.backup*`. These are likely already in `.gitignore` — verify. The SECURITY.md states Graphify "performs no network activity during graph analysis" and does not store credentials, but node label content from indexed files is extracted, so the exclusion must be confirmed.

---

### R-07 — `graphify-out/` Committed to Git (MEDIUM severity, LOW probability)

**Description:** If `graphify-out/` is not in `.gitignore`, the interactive HTML, full graph JSON, and Obsidian vault would be committed. The `graph.html` may expose repository structure more visibly than intended.

**Mitigation:** Add `graphify-out/` to `.gitignore` before the first build.

---

### R-08 — Graphify Version Drift (LOW probability, LOW impact)

**Description:** The PyPI package is `v0.9.10` as of 2026-07-08. The output format (`graph.json` schema, `GRAPH_REPORT.md` format) may change in future versions. Instructions referencing specific command flags or output paths could become incorrect.

**Mitigation:** Pin the installed version via `uv tool install graphifyy==0.9.10` or document the version used at integration time.

---

## 11. Recommended Implementation Phases

### Phase 0 — Prerequisites (no install required)

**Objective:** Make the repo ready for Graphify without running it yet.

1. Add `graphify-out/` to `.gitignore` if not already present.
2. Create `.graphifyignore` at repo root using the content in section 6.3.
3. Verify `.env` files are in `.gitignore` (they must be, per integration contract).
4. Verify `.hypogeum-assets/` is in `.gitignore`.
5. Decide whether `run_dashboard.py` should be initially excluded.

**Deliverable:** `.graphifyignore` file at repo root. `.gitignore` updated if needed. No code changes.

---

### Phase 1 — Install and First Build

**Objective:** Produce the first `graphify-out/` and assess graph quality.

1. Install: `uv tool install graphifyy`
2. Register: `graphify install`
3. From repo root: `/graphify .`
4. Read `graphify-out/GRAPH_REPORT.md` — note god nodes and communities.
5. Open `graphify-out/graph.html` — verify expected clusters (VPD, Thumbnail, Audio, Assembly, Governance, Infrastructure).
6. Run a calibration query: `graphify query "which agents call run_agent()"` — compare result against A0 audit findings.
7. Check for false inclusions (`.hypogeum-assets/` content, `.env` content) in `graph.json`.

**Acceptance criteria:**
- `graph.html` loads and shows visible communities matching A0 expected clusters
- `GRAPH_REPORT.md` identifies `pipeline.py`, `agent.py`, `config.py`, `schemas.py` as god nodes
- No `.hypogeum-assets/` paths in `graph.json`
- No API key strings in `graph.json` or `graph.html`
- Token reduction benchmark shows measurable savings vs raw file read

---

### Phase 2 — Workflow Integration

**Objective:** Establish Claude Code usage patterns for the Hypogeum project.

1. Document the staleness check procedure (section 8.5) in a project note or CLAUDE.md addendum.
2. Establish the trigger conditions (section 8.1) as working habits.
3. Run `graphify query` for the next 3–5 open-ended architecture questions and compare quality/speed vs sequential file reads.
4. Record any queries that produce poor results (INFERRED edge noise, missing nodes due to module-level constants) as known limitations.

**Acceptance criteria:**
- At least 3 architecture questions answered correctly using `graphify query` without fallback to sequential reads
- Known limitation cases documented

---

### Phase 3 — Auto-Rebuild Hook

**Objective:** Keep graph current automatically.

1. Run: `graphify hook install`
2. Verify the git commit hook is installed: `cat .git/hooks/post-commit`
3. Make a test commit and verify `graphify-out/graph.json` modification time updates.
4. If the full rebuild on every commit is too slow, switch to `--update` (incremental).

**Note:** Hook behavior is documented in the README but the exact hook implementation (pre-commit vs post-commit, `--update` vs full rebuild) is UNKNOWN from repo docs — verify against installed version.

**Acceptance criteria:**
- A commit to any `.py` or `.md` file triggers a graph update within 60 seconds
- No significant commit latency (incremental `--update` should be fast)

---

### Phase 4 — Optional: MCP Server

**Objective:** Enable persistent graph access across Claude Code sessions without re-running queries.

The repo documents `graphify serve` as an MCP stdio server that exposes the graph. This would allow Claude Code to query the graph via MCP tool calls rather than CLI invocations.

**Note:** The exact MCP server command, tool names, and schema are UNKNOWN from available repo docs. Evaluate only after Phase 3 is stable.

---

## 12. Acceptance Criteria

The Graphify integration is complete when all of the following are true:

### Correctness

- [ ] `graphify-out/` is present at repo root and excluded from git
- [ ] `.graphifyignore` excludes `.hypogeum-assets/`, `.env.*`, `*.log`, `*.db`, `graphify-out/`
- [ ] `graph.json` contains no `.hypogeum-assets/` paths
- [ ] `graph.json` contains no API key strings
- [ ] `GRAPH_REPORT.md` identifies the correct god nodes (pipeline.py, agent.py, config.py, schemas.py, run_agent, muapi.py)

### Graph Quality

- [ ] `graph.html` shows visually distinct communities matching the A0 audit clusters
- [ ] `graphify query "which agents call run_agent()"` returns all LLM-using agents identified in A0 (EP, RD, NA, SE, VPD stages, SDP, Caption, Music, Final Render Reviewer, BGA, TIG)
- [ ] `graphify path "pipeline.py" "muapi.py"` returns a valid shortest path through VPD
- [ ] `graphify explain "visual_package"` returns a meaningful summary of the schema and its producers/consumers

### Workflow Integration

- [ ] At least one open-ended codebase question answered correctly using Graphify before any file reads
- [ ] Staleness check procedure established and documented
- [ ] Known limitations (module-level constants, INFERRED edge noise) documented

### Safety

- [ ] Auto-rebuild hook active (Phase 3) OR manual rebuild documented as a required step before audit sessions
- [ ] `graphify-out/` never committed to git

---

## Appendix — Facts vs. Unknowns

### Confirmed from Repo (2026-07-09)

| Fact | Source |
|---|---|
| PyPI package name: `graphifyy` | PyPI page |
| CLI command: `graphify` | README |
| Install: `uv tool install graphifyy` | README |
| Register: `graphify install` | README |
| Invocation: `/graphify .` | README, skill.md |
| Output dir: `graphify-out/` | ARCHITECTURE.md, skill.md |
| Output files: graph.html, GRAPH_REPORT.md, graph.json, obsidian/ | skill.md, README |
| Code parsing: tree-sitter AST, no LLM, 36+ languages | README |
| Edge labels: EXTRACTED / INFERRED / AMBIGUOUS | ARCHITECTURE.md |
| `.graphifyignore` uses gitignore syntax | README |
| `.gitignore` auto-respected | README |
| Corpus limit warning: >2M words or >200 files | skill.md |
| Token reduction: "71.5x fewer tokens per query" | PyPI page |
| LOCOMO recall@10: 49.7% | README |
| LongMemEval-S QA: 76% | README |
| Auto-rebuild hook: `graphify hook install` | README |
| `--update` for incremental re-extraction | README |
| Semantic subagents: must be dispatched in parallel via Agent tool | skill.md |
| Security: no network during analysis; no code execution from source | SECURITY.md |
| Path traversal guard: outputs confined to `graphify-out/` | SECURITY.md, ARCHITECTURE.md |
| No credentials stored | SECURITY.md |

### Unknown / Not Documented in Available Sources

| Item | Status |
|---|---|
| Exact `graphify detect . --list-files` command syntax for verifying exclusions | UNKNOWN |
| Whether auto-rebuild hook is pre-commit or post-commit | UNKNOWN |
| Whether `--update` is supported in the hook | UNKNOWN |
| MCP server command and tool names | UNKNOWN |
| `graph.json` schema version and stability guarantees | UNKNOWN |
| Whether `graphify explain` is a documented subcommand | UNKNOWN (seen in README but not in skill.md constraints) |
| Behavior when a single file (e.g. run_dashboard.py at 5,600 lines) dominates graph | UNKNOWN |
| Whether `.graphifyignore` is created automatically on first run or must be created manually | UNKNOWN |
| Maximum individual file size before exclusion | UNKNOWN |
| Python version requirement | UNKNOWN |

---

*Design complete. No files were installed, modified, or created beyond this document.*
