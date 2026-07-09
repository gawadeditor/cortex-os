# Graphify-First Development Workflow

**Status:** ACTIVE — permanent workflow rule for this repository.
**Depends on:** `docs/architecture/GRAPHIFY_INTEGRATION_DESIGN.md`, `docs/architecture/GRAPHIFY_VERIFICATION_REPORT.md`

---

This document defines the standing workflow rule for any Claude session (or other AI coding assistant) working in this repository. It governs how the repo should be navigated during investigation, before any file is edited — it does not modify any agent, pipeline, or production code itself.

## The Rule

1. **Before any implementation task, run Graphify retrieval first.** Do not begin by grepping or sequentially reading files across the codebase — query the graph first.

2. **Use `graphify query "<question>"` to identify relevant files/modules.** This is the default entry point for "what touches X," "what's related to Y," or "what should I look at before changing Z" questions.

3. **Use `graphify path "A" "B"` when dependency paths matter.** Use this to confirm or discover how two files/symbols are connected before assuming a relationship exists or doesn't.

4. **Inspect only the targeted files returned by Graphify.** Once the graph has narrowed the candidate set, read those specific files directly for exact current content — the graph tells you *where* to look, never *what the code currently says*. Schemas, prompt files, skill files, `config.py` values, and test assertions must always be read from source, never inferred from the graph (per `GRAPHIFY_INTEGRATION_DESIGN.md` §9).

5. **Broad repository scans are allowed only if Graphify retrieval is insufficient.** If a query returns nothing useful, is too truncated even after narrowing with `--budget`/`--context`, or the question isn't a topology/dependency question at all, fall back to grep/Explore-style search — but only after Graphify has been tried first.

6. **After commits that affect code structure, refresh the graph before relying on it again.** Use `graphify update .` for an incremental re-extraction of changed files, or `graphify extract . --code-only` for a full rebuild if the change is large or structural. The graph is a derived snapshot, not a live view — per `GRAPHIFY_VERIFICATION_REPORT.md` §6, it has already been observed to go stale after ordinary repo-cleanup commits (moved/renamed files still show pre-move paths until refreshed). Treat "was the graph rebuilt after my last structural commit?" as a standing question before trusting a query result on anything recently touched.

7. **Browser visualization (`graph.html`) is optional and currently disabled.** This repository's graph (18,845+ nodes) exceeds Graphify's built-in 5,000-node HTML visualization safety limit. `GRAPHIFY_VIZ_NODE_LIMIT` must not be raised, and the corpus must not be narrowed, without explicit authorization in that specific session. CLI retrieval (`query`, `path`, `explain`) does not depend on the HTML view and is unaffected by this.

8. **Skills work starts only after this workflow rule is committed.** This document being committed is the explicit gate: Graphify integration (Phases 0–2) is complete, and Skills Standardization work may begin in a subsequent, separate task.

## What This Rule Does Not Change

- No agent, prompt, schema, or pipeline file is modified by this document.
- No git hooks are installed as a result of this rule (`graphify hook install` remains unused).
- No MCP server is implied or started by this rule.
- This rule does not grant standing authorization to override Graphify's own safety limits (node count, backend selection, etc.) — those remain per-session decisions.
