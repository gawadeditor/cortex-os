# Model Output Comparison Framework — Design

**Status:** Design only — no implementation  
**Date:** 2026-06-26  
**Depends on:** M7.2 provenance fields (committed), provider_comparison.py Phase 1G/1J (committed), prompt registry (Phase 2A/2B, committed)  
**Feeds into:** Dashboard Step D, provider/model selection for production runs

---

## 1. Purpose

The existing provider comparison engine (`provider_comparison.py`) generates thumbnails through configured image providers and stores results in the DB. It answers one question: **which image providers produce acceptable output?**

This framework answers a second question: **for a given agent prompt and input, which provider/model combination produces the best output — and at what cost and latency?**

The framework reuses the M7.2 provenance fields already captured in `archive_index` (generation_prompt, model, provider, cost_usd, archive_location) to surface historical comparison data without new generation. When new comparison runs are needed, it gates them behind an explicit operator approval step.

---

## 2. Comparison Run Object

A `ModelComparisonRun` represents one operator-initiated comparison session.

```
ModelComparisonRun:
  run_id            TEXT  PK — uuid4
  run_type          TEXT  — "llm" | "image" | "video"
  prompt_name       TEXT  — stem of the registered prompt file (e.g. "executive_producer");
                            NULL for archive-sourced runs
  prompt_text       TEXT  — the exact prompt text submitted to each provider
  input_context     TEXT  — JSON: any agent-specific input sent alongside the
                            prompt (e.g. topic_brief JSON for EP, null for image)
  provider_matrix   TEXT  — JSON array of {provider, model} pairs tested
  status            TEXT  — "running" | "completed" | "cancelled" | "error"
                            (approval happens in the browser before POST; no DB-level
                            pending_approval state is needed)
  initiated_at      TEXT  — ISO timestamp when operator clicked "Approve and Run"
  completed_at      TEXT  — ISO timestamp of last result received
  operator_note     TEXT  — optional free-text note captured in approval modal
  results_json      TEXT  — JSON array of ModelComparisonResult inline (v1);
                            stored as a single JSON blob, matching the column name
                            used by the existing provider_comparison_runs table.
                            Separate result rows are deferred to v2.
```

---

## 3. Input Prompt Selection

Two sources in v1:

### 3a. From Prompt Registry (primary)
The operator selects a registered prompt by name from `GET /api/prompts`. The framework uses `get_prompt(name)` to load the body and submits it via `call_claude()` (from `hypogeum/openrouter.py`) as the `system` message. `run_agent()` is **not** used — it has side effects (disk writes, schema validation, episode_id coupling) that are inappropriate for a comparison run. `call_claude()` returns raw text directly.

**`Config` instantiation:** the comparison engine constructs `Config()` at call time within the dashboard route handler — the same pattern used by any other route that needs API access.

Fixed user messages per prompt type:
- LLM agent prompts (`executive_producer`, `research_director`, etc.): canonical benchmark topic — "The Antikythera Mechanism: ancient Greece's computing wonder" — as the `user` message. Same topic as `ep_val_01`.
- Image prompts (`thumbnail_director.txt`): use `COMPARISON_PROMPT` from `provider_comparison.py`.

### 3b. Custom prompt *(deferred to v2)*
A `<textarea>` for arbitrary system prompt + user message is useful but adds UI complexity and a `prompt_name = NULL` edge case. Deferred.

### 3c. From archive (zero-cost, read-only)
The operator selects an existing `episode_id` and the framework queries `archive_index` for all rows matching that episode. Provenance fields (generation_prompt, model, provider, cost_usd) are surfaced directly — no new generation.

**Archive null caveat:** The M7.2 provenance columns (`generation_prompt`, `generation_params`, `cost_usd`, etc.) are added via V3 column migration and are NULL for any episode that ran before M7.2 was committed. `ep_val_01` was run before M7.2 — its archive rows will have `generation_prompt IS NULL`. The archive panel must handle this gracefully (show "No provenance data for this episode — run a new episode to populate comparison data") rather than showing an empty table silently.

---

## 4. Provider/Model Pair Matrix

The matrix defines which (provider, model) pairs are tested in a single run. It is configured per run type:

### LLM runs
| Provider | Model slug | Notes |
|---|---|---|
| openrouter | `anthropic/claude-sonnet-4.6` | Current default for most agents |
| openrouter | `anthropic/claude-opus-4.6` | Highest capability, higher cost (matches `_DEFAULT_OPUS_SLUG` in config.py) |
| openrouter | `openai/gpt-5.5-pro` | TD planning model default |
| openrouter | `meta-llama/llama-3.3-70b-instruct` | Cost comparison point |

### Image runs (existing `DEFAULT_IMAGE_PROVIDERS`)
| Provider | Model slug |
|---|---|
| openai | `gpt-image-1` |
| openrouter | `openai/gpt-5-image-mini` |
| muapi | `gpt-image-2-text-to-image` |
| freepik | `flux-dev` |

### Video runs (existing `DEFAULT_VIDEO_PROVIDERS`)
Credential probe only (no generation) — reuses Phase 1J readiness check.

The operator can deselect individual pairs on the approval screen before the run is triggered.

---

## 5. Output Artifact Schema

Each element of the `ModelComparisonRun.results` JSON array is a `ModelComparisonResult` object.
Results are stored as a JSON blob in the `results` column of `model_comparison_runs` — not as
separate table rows in v1. Separate rows and a quality-rating endpoint are deferred to v2.

```
ModelComparisonResult (v1 — JSON element in results column):
  provider          TEXT  — e.g. "openrouter"
  model             TEXT  — e.g. "anthropic/claude-sonnet-4.6"
  status            TEXT  — "success" | "error" | "skipped_no_key"
  latency_ms        INT   — wall-clock time from request to first token / full response
  cost_usd          REAL  — actual cost from finance.record_cost, or estimated if unavailable
  prompt_text       TEXT  — exact system + user message submitted
  output_text       TEXT  — full text response (LLM); NULL for image/video
  output_path       TEXT  — file path for image/video output; NULL for LLM
  output_tokens     INT   — response token count (LLM only)
  error             TEXT  — error message if status = "error"
  credential_source TEXT  — "env" | "secret_profile" | "none"
  archive_asset_id  TEXT  — archive_index.asset_id if sourced from archive; NULL for live runs

Deferred to v2:
  quality_notes     TEXT  — operator free text (requires rating endpoint)
  quality_rating    INT   — operator 1–5 rating (requires rating endpoint)
```

---

## 6. Reading M7.2 Provenance Fields

The M7.2 pass added these columns to `archive_index`:

| Column | Type | Source |
|---|---|---|
| `generation_prompt` | TEXT | Prompt submitted to provider |
| `generation_params` | TEXT | JSON: aspect_ratio, duration_seconds, tier_used, etc. |
| `cost_usd` | REAL | Per-asset generation cost |
| `scene_id` | TEXT | Scene identifier |
| `archive_location` | TEXT | Current file path |
| `version` | INTEGER | Generation attempt index |
| `asset_created_at` | TEXT | ISO timestamp of generation |

### Archive query for zero-cost comparison

```sql
SELECT
    a.asset_id,
    a.provider,
    a.model,
    a.generation_prompt,
    a.generation_params,
    a.cost_usd,
    a.archive_location,
    a.asset_created_at,
    a.scene_id
FROM archive_index a
WHERE a.episode_id = ?
  AND a.generation_prompt IS NOT NULL
  AND a.provider IS NOT NULL
ORDER BY a.asset_created_at DESC
```

This is the primary read path. The comparison framework surfaces this data before offering to run new generation.

**Null provenance caveat:** The M7.2 provenance columns (`generation_prompt`, `generation_params`, `cost_usd`, `archive_location`, `version`, `asset_created_at`) are V3 migration columns, not in the base `schema.sql` definition. They are NULL for any episode generated before M7.2 was committed. `ep_val_01` (the only partially-generated real episode as of this writing) was run before M7.2 — its `archive_index` rows will have `generation_prompt IS NULL` and will be excluded by the `WHERE generation_prompt IS NOT NULL` filter. The archive panel will show zero rows for `ep_val_01`. The first post-M7.2 episode will populate these columns and make the archive source useful.

### Cross-referencing with costs

```sql
SELECT
    c.provider,
    c.model,
    c.cost_usd,
    c.tokens_or_credits,
    c.call_type,
    c.called_at,
    c.asset_id
FROM costs c
WHERE c.asset_id = ?
```

The `asset_id` column links `costs` rows to `archive_index` rows, allowing exact cost attribution per output asset. Note: the table is named `costs`, not `cost_tracking`.

---

## 7. Dashboard Surface Layout

### 7a. New route: `GET /compare`

A dedicated comparison page separate from `/providers` (which focuses on credential and readiness state).

```
┌─────────────────────────────────────────────────────┐
│ Model Output Comparison                              │
│ ─────────────────────────────────────────────────── │
│                                                      │
│ [Source]  ○ Archive (ep_val_01)  ○ Registry  ○ Custom│
│                                                      │
│ [Prompt]  executive_producer  ▼  (from registry)     │
│                                                      │
│ [Matrix]  ☑ claude-sonnet-4.6  ☑ gpt-5.5-pro        │
│           ☑ claude-opus-4.6    ☐ llama-3.3-70b       │
│                                                      │
│ [Estimated cost: $0.12]  [Run Comparison →]          │
│                                                      │
├─────────────────────────────────────────────────────┤
│ Results — Last run: 2026-06-26 14:32 UTC             │
│                                                      │
│ Provider/Model       Latency  Cost    Status  Rating │
│ ──────────────────────────────────────────────────── │
│ openrouter/sonnet    1.2s     $0.02   ✓       ★★★★☆ │
│ openrouter/opus      3.8s     $0.08   ✓       ★★★★★ │
│ openrouter/gpt-5.5   2.1s     $0.04   ✓       ★★★☆☆ │
│                                                      │
│ [▶ claude-sonnet-4.6 output]  [▶ gpt-5.5-pro output] │
└─────────────────────────────────────────────────────┘
```

Output panels use `<details>/<summary>` (same pattern as `/prompts`) for collapsible text output. Image outputs show thumbnails. Quality rating and notes fields are editable inline and saved via `POST /api/compare/<run_id>/<result_id>/rate`.

### 7b. Archive panel

When source = Archive, the page renders existing provenance data from `archive_index` directly — no API calls, no generation. The "Run Comparison" button is replaced with "New Run (costs ~$X)".

### 7c. Nav placement

Add `Comparison` link to the Operations dropdown in `_nav.html`, alongside Prompt Registry.

---

## 8. Operator Approval Flow

Generation runs cost real money. The flow has three stages:

### Pre-run: cost estimate display

Before the run is triggered, the page displays:
1. The selected provider/model matrix
2. An estimated cost range, derived from `MODEL_CREDIT_COSTS` in `muapi.py` and existing `costs` table averages — the same sources used by the existing comparison engine
3. The estimated duration

The "Run Comparison" button is always enabled. The cost estimate is informational — the modal is the only approval gate. There is no "acknowledge to unlock" mechanism.

The estimate is conservative (uses maximum known cost per provider/model).

### Confirmation modal

Clicking "Run Comparison →" opens a confirmation modal before any API call:
```
Run Model Output Comparison?

Prompt:    executive_producer (613 chars)
Models:    claude-sonnet-4.6, claude-opus-4.6, gpt-5.5-pro
Estimated: $0.12 – $0.18
Duration:  30–120 seconds

[Cancel]  [Approve and Run →]
```

"Approve and Run" triggers `POST /api/compare/run`. No generation starts without this explicit click. The `initiated_at` timestamp is written to the DB when the POST is received.

### Runtime: credential check

Before the first API call, the engine confirms each provider has a valid credential (same logic as `provider_comparison.py:_resolve_credential`). Any provider with no credential is marked `skipped_no_key` and shown in the results — no silent failures.

---

## 9. Test Plan (for later implementation)

```
tests/test_model_comparison.py

TestComparisonRunSchema
  test_run_object_has_required_fields
  test_result_object_has_required_fields
  test_status_values_are_constrained

TestArchiveQuery
  test_archive_query_returns_provenance_fields
  test_archive_query_filters_by_episode_id
  test_archive_query_excludes_rows_without_prompt
  test_cross_reference_with_costs

TestComparisonApprovalFlow
  test_run_not_created_without_approval
  test_approval_sets_initiated_at
  test_missing_credential_sets_skipped_no_key
  test_cost_estimate_shown_before_run

TestCompareDashboardPage
  test_compare_page_returns_200
  test_compare_page_shows_heading
  test_compare_page_shows_archive_source_option
  test_compare_page_shows_registry_source_option
  test_compare_page_shows_provider_matrix
  test_compare_page_shows_estimated_cost
  test_compare_page_requires_approval_before_run
  test_compare_page_shows_results_when_run_exists
  test_compare_page_archive_panel_handles_null_provenance
  # deferred to v2: test_compare_page_shows_quality_rating_fields
  # deferred to v2: test_rate_endpoint_saves_quality_note

TestNoUnauthorisedGeneration
  test_post_run_without_approval_returns_403
  test_run_does_not_call_providers_when_source_is_archive
```

---

## 10. Files to Create / Modify

### New files

| File | Purpose |
|---|---|
| `hypogeum/model_comparison.py` | `ModelComparisonRun`, `ModelComparisonResult` dataclasses; `run_model_comparison()` via `call_claude()` (not `run_agent()`); `get_model_comparison_result()`; `query_archive_for_comparison()` with null-provenance handling |
| `hypogeum/templates/compare.html` | Comparison dashboard page (archive panel, prompt registry selector, model matrix checkboxes, cost estimate display, approval modal, collapsible results panel) |
| `tests/test_model_comparison.py` | Full test suite per §9 above |

### Modified files

| File | Change |
|---|---|
| `hypogeum/schema.sql` | Add `model_comparison_runs` table (single table; results stored as JSON column — no separate results table in v1). New tables are added as `CREATE TABLE IF NOT EXISTS` directly in schema.sql — no column migration needed. |
| `hypogeum/db.py` | Add helper functions only: `insert_model_comparison_run()`, `complete_model_comparison_run()`, `get_latest_model_comparison()`. No column migrations — `_Vn_COLUMN_MIGRATIONS` are for adding columns to existing tables; new tables go in schema.sql. |
| `run_dashboard.py` | Add `GET /compare` route; `POST /api/compare/run`; `GET /api/compare/results` |
| `hypogeum/templates/_nav.html` | Add `Comparison` link in Operations dropdown |

### Unchanged files

| File | Reason |
|---|---|
| `hypogeum/provider_comparison.py` | Image/video comparison engine is separate; model_comparison.py is a new parallel module, not a replacement |
| `hypogeum/openrouter.py` | `call_claude()` is the LLM comparison interface (read dependency, not modified). `run_agent()` is NOT used — it writes files, validates schema, and requires episode_id coupling inappropriate for a comparison context. |
| `hypogeum/asset_registry.py` | M7.2 provenance fields already written; comparison only reads them |
| `hypogeum/agents/*.py` | No changes — comparison does not invoke agents |
| `hypogeum/prompts/*.txt` | Read-only inputs; prompt body loaded via `get_prompt()` from prompt_registry |

---

## 11. Implementation Sequence (when approved)

```
1. schema.sql   — ADD model_comparison_runs table (CREATE TABLE IF NOT EXISTS;
                  no column migration needed; results stored as JSON column)
2. db.py        — helper functions: insert_model_comparison_run(),
                  complete_model_comparison_run(), get_latest_model_comparison()
3. model_comparison.py — archive query path (null provenance handled) +
                  call_claude() LLM comparison; approval-gated execution
4. tests/test_model_comparison.py — archive (null handling), approval flow, schema
5. compare.html + GET /compare    — archive panel + prompt registry selector
6. POST /api/compare/run          — live LLM generation with approval gate
7. Full test pass

Deferred to v2:
  - quality rating endpoints (POST /api/compare/<run_id>/<result_id>/rate)
  - custom prompt textarea
  - separate model_comparison_results table
```

Archive reads are implemented before any live generation so operators can see historical provenance data at zero cost before committing to a paid comparison run. Note that the archive panel will be empty until the first post-M7.2 episode is generated.
