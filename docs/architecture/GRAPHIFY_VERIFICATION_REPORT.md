# Graphify Verification Report

**Date:** 2026-07-10
**Scope:** Read-only verification of the Graphify install, graph build, and CLI retrieval layer for this repository.
**Status:** COMPLETE

---

## 1. Install Status

- Canonical upstream source, per explicit user confirmation: `https://github.com/safishamsi/graphify`
- Installed via `uv tool install graphifyy` (the officially recommended isolated-environment method). System Python was 3.9.6, below Graphify's `>=3.10` requirement, so `uv` was installed via Homebrew first to provide an isolated compatible runtime — no system Python packages were touched.
- Installed package: `graphifyy==0.9.11`, exposing two executables: `graphify`, `graphify-mcp`.
- `graphify install` (skill registration) was **not run**. `graphify hook install` (git hooks) was **not run**. The MCP server (`graphify-mcp` / `python -m graphify.serve`) was **not started**. All three were explicitly out of scope for this phase.

## 2. Build Status

- Build command: `graphify extract . --code-only`
- `--code-only` restricts extraction to local tree-sitter AST parsing — no LLM/semantic backend, no API key required, nothing leaves the machine.
- Result: 418 code files parsed, **18,845 nodes**, **51,871 edges**, **576 communities** detected; 18 duplicate nodes merged (4 exact, 14 fuzzy); 196 non-code files (docs) correctly skipped per `--code-only`; 7 files produced zero nodes (plain data JSON files with no code structure — expected, not an error).
- `.graphifyignore` (repo root, created in Phase 0) was respected — excluded `.hypogeum-assets/`, `.env*`, logs, DBs, and root-level image noise from consideration.
- **Graph build timestamp: 2026-07-10 01:21:42.** The graph has **not been rebuilt** since. Several repo-cleanup commits (operational tools commit, workspace-artifact archive commit) landed after this timestamp — see §6.

## 3. Graph Output Files

```
graphify-out/
├── graph.json                 27.1 MB — the queryable graph (primary artifact)
├── GRAPH_REPORT.md            180 KB  — plain-language highlights, communities, suggested questions
├── .graphify_analysis.json     1.4 MB — community/analysis intermediate data
├── .graphify_labels.json       13 KB  — community label data (placeholders — see below)
├── .graphify_labels.json.sig   15 KB  — label integrity signature
├── manifest.json               77 KB  — file-level extraction manifest
└── cache/
    └── stat-index.json                — incremental-build cache
```

**`graph.html` still does not exist.** `graphify cluster-only . --no-label` was run (the official command that regenerates both `graph.html` and `GRAPH_REPORT.md`). `GRAPH_REPORT.md` was generated successfully — 573 communities (down from 576 pre-relabel; re-clustering is non-deterministic at the margins). `graph.html` was **not** generated: the tool refused with `Skipped graph.html: Graph has 18845 nodes - too large for HTML viz (limit: 5000). Use --no-viz, raise GRAPHIFY_VIZ_NODE_LIMIT, or reduce input size.` This is the tool's own built-in safety guard against an unusably heavy browser render, not a bug or an omitted step. `--no-label` was used, so no LLM/API backend was invoked at any point — communities are still numeric-ID placeholders, not human-named.

## 4. Browser Graph Status

**Not opened — confirmed not generated, not merely skipped.** `open graphify-out/graph.html` was attempted after the `cluster-only` run and failed: `The file .../graphify-out/graph.html does not exist.` (exit 1). Producing it would require explicitly raising `GRAPHIFY_VIZ_NODE_LIMIT` above 18,845 (or reducing the indexed corpus), which was not done — that's a scope decision for a future session, not an oversight here, since overriding a tool-imposed safety limit wasn't explicitly authorized.

## 5. Retrieval Query Results Summary

All three CLI checks executed successfully with real, grounded output:

| Query | Result |
|---|---|
| `graphify query "Which files should be inspected before editing the Visual Package Director?"` | 1,116 nodes found (BFS depth=2). Correctly centered on `run_visual_package_director()` in `hypogeum/agents/visual_package_director.py`, surfacing real direct dependencies: `pipeline.py`, `schemas.py`, `muapi.py`, `agent.py`, `config.py`, `openrouter.py`, plus adjacent agents it interacts with (`thumbnail_director.py`, `brand_guardian_agent.py`, `assembly_agent.py`). |
| `graphify query "Which files are related to skill loading and prompt composition?"` | 380 nodes found. Correctly surfaced `prompt_loader.py` (`load_prompt()`), `compose_system_prompt()`-adjacent agents (`visual_package_director.py`, `thumbnail_prompt_writer.py`), and `prompt_registry.py`. |
| `graphify path "run_worker.py" "hypogeum/orphan_runs.py"` | Resolved: 3-hop path `run_worker.py --imports--> get_connection() <--imports-- orphan_runs.py <--rationale_for-- hypogeum/orphan_runs.py — Orphaned run reconciliation...`, all edges `EXTRACTED`-confidence (highest tier — explicit in source, not inferred). |

These results are consistent with the retrieval verification performed earlier in Phase 2 (6/6 queries then; 3/3 here), confirming the CLI retrieval layer is stable and repeatable.

## 6. Weak Points / Limitations

- **Graph staleness (confirmed, not hypothetical).** The graph was built at 01:21:42 on 2026-07-10. Since then, two commits changed the working tree: `c810422` (added `run_e2e_lite.py`, `tools/qa_episode.py`, `tools/inject_normalized_missing_scenes.py`) and `23b1211` (moved 15 files, including `experiments/skill_prompt_compare_v1.py` and four `scripts/`/`tools/` files, into `_archive/stale-untracked-2026-07-10/`). The graph still reflects the **pre-move** paths — e.g. query results above still show `NODE skill_prompt_compare_v1.py [src=experiments/skill_prompt_compare_v1.py ...]`, which is no longer the file's real location. `graphify update .` (incremental) or `graphify extract . --code-only` (full rebuild) is needed before the graph can be trusted for path-sensitive answers about anything touched by those two commits. This is the exact R-01 risk anticipated in `GRAPHIFY_INTEGRATION_DESIGN.md`.
- **Default query budget (2000 tokens) truncates hard** on a repo this size — every query so far has cut hundreds of nodes (304–1,036 in this run). Usable for orientation, but `--budget` or `--context` narrowing is needed for precision work rather than relying on the bare default.
- **Node-name resolution is fuzzy and not always intuitive** — a bare filename (`orphan_runs.py`) and a path-qualified name (`hypogeum/orphan_runs.py`) can resolve to different node variants (one to the module node, one to a docstring/rationale node), as seen consistently across two separate verification passes.
- **`GRAPH_REPORT.md` now exists, but communities remain unnamed** — `cluster-only` was run and produced the report, but `--no-label` was used deliberately to avoid invoking an LLM backend, so communities are still numeric IDs (e.g. `community=13`), not human-readable labels. Higher-level "what part of the codebase is this" framing is weaker than it could be until `graphify label .` is run with an approved backend.
- **No visualization available, and not simply pending** — `graph.html` generation was attempted and refused by the tool itself (18,845 nodes exceeds its 5,000-node built-in limit). CLI-only retrieval for now unless the corpus is narrowed or the limit is explicitly raised in a future session.

## 7. Final Verdict

**READY** — for CLI-based topology/dependency retrieval (query, path, explain), with the caveat that the graph must be refreshed (`graphify update .`) before being trusted against any file touched by commits made after 2026-07-10 01:21:42. **NOT READY** for browser visualization specifically — `graph.html` is blocked by the tool's own 5,000-node limit at this repo's current size (18,845 nodes) and would require an explicit, separately-authorized decision to raise `GRAPHIFY_VIZ_NODE_LIMIT` or narrow the indexed corpus. This does not affect the CLI-first workflow below, which does not depend on the HTML view.

## 8. Required Future Workflow Rule

> **Before any code change, use Graphify first to identify relevant files, dependencies, and affected modules. Then inspect only targeted files. Broad repository scans are allowed only if Graphify retrieval is insufficient.**

This rule is recorded here as the standing instruction for future Claude sessions working in this repository. It does not modify any agent, pipeline, or production code — it governs how a Claude session should navigate the repo during its own investigation, prior to editing anything.
