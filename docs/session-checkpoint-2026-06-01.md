# Session Checkpoint — 2026-06-01

**Created:** End of M0 implementation session — 2026-06-01
**Purpose:** Allow a new session to resume at M1 with zero context loss.
**Project:** hypogeum-brain — fully automated YouTube production pipeline
**Workspace:** `/Users/gawadeditor/hypogeum-brain`

---

## 1. Current Project Status

| Phase | Status | Completion |
|---|---|---|
| Architecture design | **Complete** | 100% — all specs frozen |
| Integration specifications | **Complete** | 100% — all 7 integration specs frozen |
| M0 — Foundation | **Complete** | 100% — 483 tests passing |
| M1 — Text Pipeline | **In progress** | M1.1 Perplexity started |
| M2–M5 | Not started | — |

**Overall estimated project completion:** ~40%

---

## 2. M0 Completion Summary

M0 is fully complete. All 6 deliverables implemented and tested.

### Modules Implemented

| Module | File | Purpose | Tests |
|---|---|---|---|
| Config | `hypogeum/config.py` | Environment loading, milestone validation | 40 |
| Logger | `hypogeum/logger.py` | Structured JSON logging, get_logger() | 35 |
| OpenRouter | `hypogeum/openrouter.py` | Claude call routing, retry, fallback to Anthropic direct | 90 |
| Schemas | `hypogeum/schemas.py` | Validators for all 11 frozen output packages | 125 |
| Agent Runner | `hypogeum/agent.py` | run_agent() — parse, validate, write, log | 61 |
| Startup | `hypogeum/startup.py` | Health checks, StartupReport, fail-fast | 62 |
| Pipeline | `hypogeum/pipeline.py` | 9-agent orchestrator scaffold, all stubs | 68 |
| CLI | `run_pipeline.py` | Entry point, argument parsing, exit codes | (in pipeline tests) |

**Total M0 test count: 483**
**Test files:** `tests/test_config.py`, `tests/test_logger.py`, `tests/test_openrouter.py`, `tests/test_schemas.py`, `tests/test_agent.py`, `tests/test_startup.py`, `tests/test_pipeline.py`

### Integration Contracts (must not be violated)

- All environment access through `Config` — never `os.getenv()`
- All logging through `hypogeum.logger.get_logger()` — never `print()` or stdlib logging
- All Claude calls through `hypogeum.openrouter.call_claude()` — never `requests` directly
- All schema validation through `hypogeum.schemas.validate()` — no duplicate checks
- Startup verification through `hypogeum.startup.run_startup_checks()` — no bypass
- All agent calls through `hypogeum.agent.run_agent()` — no direct `call_claude()` in agents

### Model Assignments (frozen — T0.1)

| Model | Slug | Agents |
|---|---|---|
| Claude Sonnet 4.6 | `anthropic/claude-sonnet-4.6` | EP, SE, VPD, TD, Audio, Assembly, Publishing |
| Claude Opus 4.6 | `anthropic/claude-opus-4.6` | Research Director, Narrative Architect |

### Key Architecture Decisions (frozen)

- OpenRouter primary; Anthropic direct fallback on 401 or retry exhaustion
- `_use_anthropic_for_run` flag in openrouter module — set by startup or 401, cleared by `reset_run_state()`
- Retry policy: max 1 retry, 5s initial, 2× multiplier, 30s cap, ±15% jitter
- Hard failures (400/402/403/content_filter): no retry, no fallback
- Soft failures (429/5xx/timeout): retry then fallback
- File write is atomic with validation — file not written unless parse AND schema both pass
- Output path: `.hypogeum-assets/episodes/{episode_id}/{schema}_{episode_id}.json`

---

## 3. Dependency List (as of M0 complete)

**Runtime:**

| Package | Version | Purpose |
|---|---|---|
| `requests` | 2.32.5 | HTTP client for OpenRouter, Anthropic, Perplexity, all providers |

**Dev/test:**

| Package | Version | Purpose |
|---|---|---|
| `pytest` | 8.4.2 | Test runner |

**Python stdlib only:** `json`, `time`, `random`, `datetime`, `dataclasses`, `pathlib`, `os`, `sys`, `typing`

**No external JSON Schema library.** Schema validation is stdlib-only (custom validators in `hypogeum/schemas.py`).

---

## 4. Pipeline Stub Status

All 9 agent stubs are in `hypogeum/pipeline.py`. Each raises `NotImplementedError` until implemented.

| Agent | Stub Function | Milestone | Status |
|---|---|---|---|
| Executive Producer | `_run_executive_producer` | M1.2 | Stub |
| Research Director | `_run_research_director` | M1.3 | Stub |
| Narrative Architect | `_run_narrative_architect` | M1.4 | Stub |
| Script Engine | `_run_script_engine` | M1.5 | Stub |
| Visual Package Director | `_run_visual_package_director` | M2.3 | Stub |
| Thumbnail Director | `_run_thumbnail_director` | M2.5 | Stub |
| Audio Agent | `_run_audio_agent` | M3 | Stub |
| Assembly Agent | `_run_assembly_agent` | M4 | Stub |
| Publishing Agent | `_run_publishing_agent` | M5 | Stub |

---

## 5. M1 Implementation Plan

### M1.1 — Perplexity Integration (IMMEDIATE — in progress)

**File:** `hypogeum/perplexity.py`
**Spec:** `.octogent/tentacles/provider-integrations/perplexity-integration.md` (frozen v1)
**Test file:** `tests/test_perplexity.py`

Key behaviors:
- `query_perplexity(query, config, ...)` — POST to `https://api.perplexity.ai/chat/completions`
- `check_perplexity_health(config, ...)` → `"available"` / `"degraded"` / `"unavailable"`
- System prompt embedded in client (prioritize academic sources, historical accuracy)
- Sequential only — no parallel calls
- Max 1 retry per query; 5s initial delay, 2× multiplier, 60s cap, ±20% jitter
- Hard failures: 400, 401, 403, 422 — no retry, raise immediately
- Soft failures: 429, 500, 503, finish_reason:length, finish_reason:content_filter, empty choices — retry once
- 429: use `Retry-After` header (default 60s if absent)
- finish_reason:length: double max_tokens on retry
- finish_reason:content_filter: append academic-framing suffix to query on retry
- Never log API key; log query at debug level only (200-char limit)

**Estimated test additions:** ~70

### M1.2 — Executive Producer (after M1.1)

**Stub to replace:** `_run_executive_producer` in `hypogeum/pipeline.py`
**Output:** (validation_scorecard, topic_brief) — two `run_agent()` calls
**System prompt:** load from `hypogeum/prompts/executive_producer.txt` (to be created)

### M1.3 — Research Director (after M1.1 complete and tested)

**Stub to replace:** `_run_research_director`
**Depends on:** Perplexity client (M1.1) + Claude Opus (via run_agent)
**Output:** (research_package, Optional[pdr])

### M1.4 — Narrative Architect (after M1.3)

**Stub to replace:** `_run_narrative_architect`
**Model:** Opus 4.6

### M1.5 — Script Engine (after M1.4)

**Stub to replace:** `_run_script_engine`
**Model:** Sonnet 4.6

---

## 6. Active Work Items

1. **IMMEDIATE:** Implement M1.1 Perplexity integration (`hypogeum/perplexity.py`)
2. **Blocked on M1.1:** Research Director implementation (M1.3)
3. **P3 (parallel):** MUAPI capability verification — should begin during M1 to avoid blocking M2

---

## 7. Known Constraints

- `ELEVENLABS_VOICE_ID` must be pinned before M3 — irreversible identity decision
- MUAPI P3 capability verification must happen during M1 (determines Path A/B/C for entity consistency)
- YouTube OAuth requires interactive browser authorization — must be done before M5
- Meta access token has 60-day expiry — set a calendar reminder
- TikTok API availability uncertain — expected graceful failure
- Do not build topic discovery tooling in Phase 1 — topic is pre-selected (e.g., "The Antikythera Mechanism")

---

## 8. File Tree (as of M0 complete)

```
hypogeum-brain/
├── hypogeum/
│   ├── __init__.py
│   ├── config.py           ← M0 complete
│   ├── logger.py           ← M0 complete
│   ├── openrouter.py       ← M0 complete
│   ├── schemas.py          ← M0 complete
│   ├── agent.py            ← M0 complete
│   ├── startup.py          ← M0 complete
│   └── pipeline.py         ← M0 complete (stubs for all 9 agents)
├── tests/
│   ├── test_config.py
│   ├── test_logger.py
│   ├── test_openrouter.py
│   ├── test_schemas.py
│   ├── test_agent.py
│   ├── test_startup.py
│   └── test_pipeline.py
├── run_pipeline.py         ← CLI entry point
├── pytest.ini
└── .env.example            ← (not yet created; add before first real run)
```

---

## 9. Quality Bar

**Maintained throughout M0:** 483/483 passing, zero skips, zero xfail, zero warnings.

This bar must be maintained through M1–M5. Every new module gets its own test file. All external calls (Perplexity, MUAPI, ElevenLabs, platform APIs) are mocked in unit tests. Integration tests hitting real APIs are opt-in only (environment flag gate).
