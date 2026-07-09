# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What This Is

**Hypogeum Brain** is an autonomous multi-channel media factory platform. It discovers topics, produces documentary-style YouTube episodes end-to-end (research → script → voice → video → thumbnail → assembly → publish), and learns from analytics to improve future episodes. The "Hypogeum Chronicles" YouTube channel is the first template instance.

The system is **not** a YouTube tool — it is a factory OS for autonomous media production at scale. New channels are created by cloning the Hypogeum template with different brand/topic configurations.

---

## Dashboard Operator Workflow

The dashboard (`run_dashboard.py`) is the primary way to operate the factory. The intended daily workflow is entirely dashboard-driven:

```
/discover  →  Score & Rationale  →  Queue with provenance  →  /create  →  Track episode
```

1. **`/discover`** — Run a discovery cycle (background thread; browser-independent). Each candidate shows a score bar, click/retention/watch-time rationale, and risk notes. Approve a candidate to queue it with provenance (`source=discovered`, `discovery_score`, `candidate_id` written to `episode_topics`).

2. **`/create`** — Production queue is the primary section. Each queued topic shows its source (discovered vs manual), discovery score, and date added. Click **▶ Start** to launch a run. Manual topic entry is a **secondary collapsible override** (`<details>`), not the primary CTA.

3. **`/runs` and `/live`** — Track in-progress and completed production runs.

4. **`/analytics`** — View real YouTube analytics (`source='api'` rows only). Mock data is never shown as real KPIs.

5. **`/discover` → `/create` shortcut** — Candidates in the queue panel at the bottom of `/discover` have their own **▶ Start** buttons. Approved and queued candidates display a "✓ queued" badge inline.

`run_pipeline.py` is an **emergency/debugging tool** for running a single episode from the command line. It is not the normal production path.

---

## Commands

### Run tests
```bash
python3 -m pytest                          # full suite
python3 -m pytest tests/test_pipeline.py  # single file
python3 -m pytest tests/test_pipeline.py::TestStartupIntegration::test_startup_error_propagates  # single test
python3 -m pytest -q --tb=short           # quiet with short tracebacks
```

### Dashboard (primary operator UI)
```bash
python3 run_dashboard.py             # http://127.0.0.1:5001
python3 run_dashboard.py --port 5002
python3 run_dashboard.py --host 0.0.0.0
```

### Autonomous worker (scheduler → queue → pipeline loop)
```bash
python3 run_worker.py                # live mode, 60s interval
python3 run_worker.py --once         # single iteration then exit
python3 run_worker.py --dry-run      # tick + promote but never call pipeline
python3 run_worker.py --interval 30
```

### Single episode (emergency/debugging only — use the dashboard queue in normal operation)
```bash
python3 run_pipeline.py --topic "The Antikythera Mechanism" --episode-id ep_001
python3 run_pipeline.py --topic "..." --episode-id ep_001 --channel-id hypogeum --youtube-privacy private
```

### Acceptance / preflight
```bash
python3 run_acceptance_audit.py
python3 run_production_readiness_audit.py
python3 scripts/bootstrap_channel.py        # idempotent channel init
python3 scripts/bootstrap_channel.py --verify-only
```

---

## Architecture

### Entry Points
- `run_dashboard.py` — Flask web server (5,600+ lines). All dashboard routes and API endpoints live here.
- `run_pipeline.py` — CLI for a single manual episode run.
- `run_worker.py` — Long-running autonomous loop: scheduler → queue → pipeline.

### Pipeline Execution Order (frozen)
```
EP → RD → NA → SE → VPD → TD → Audio → Assembly → Publishing
```
1. **Executive Producer** — scores topic (ACCEPT/WARN/REJECT), produces `topic_brief`
2. **Research Director** — Perplexity queries, produces `research_package`
3. **Narrative Architect** — story structure, produces `narrative_package`
4. **Script Engine** — narration beats + visual directions, produces `script_package`
5. **Visual Package Director** — plans scenes (Phase 1: Claude), generates video clips (Phase 2: MUAPI), produces `visual_package`
6. **Thumbnail Director** — generates thumbnail candidates, produces `thumbnail_package`
7. **Audio Agent** — ElevenLabs narration + music, produces `narration_package`
8. **Assembly Agent** — FFmpeg render, produces `final_render_package`
9. **Publishing Agent** — YouTube upload, produces `publishing_record`

Every agent output is validated by `hypogeum/schemas.py` (26 schemas) before passing to the next stage. A failed validation raises `SchemaError` and halts the pipeline.

### Key Modules
| Module | Role |
|---|---|
| `hypogeum/pipeline.py` | Orchestrator — calls all 9 agents in order, handles fail-fast exits |
| `hypogeum/agent.py` | Central LLM runner — serialize → call OpenRouter → validate → write disk |
| `hypogeum/config.py` | Single read point for all env vars. **Never call `os.getenv()` directly.** |
| `hypogeum/db.py` | SQLite write helpers. `initialize_schema()` is idempotent. DB: `.hypogeum-assets/hypogeum.db` |
| `hypogeum/schema.sql` | All table DDL. Migrations via `_V2_COLUMN_MIGRATIONS` dict in `db.py`. |
| `hypogeum/muapi.py` | MUAPI aggregator client for video/image generation. All VPD and TD media calls go through here. |
| `hypogeum/openrouter.py` | OpenRouter client with Anthropic fallback. All Claude calls go through here. |
| `hypogeum/logger.py` | Structured JSONL logger. Log to `.hypogeum-assets/pipeline.log`. **Never use `print()` or stdlib logging.** |
| `hypogeum/topic_discovery.py` | Discovery cycle engine. `run_discovery_cycle()` dispatches to v1 (heuristic) or v2 (LLM+Perplexity) based on `DISCOVERY_ENGINE` env var. |
| `hypogeum/dashboard_control.py` | Action wrappers imported by `run_dashboard.py` (approve, reject, enqueue_topic, etc.). |
| `hypogeum/topics.py` | `enqueue_topic()` — adds to `episode_topics` queue with provenance (source, discovery_score, candidate_id). |

### Discovery System
- **`DISCOVERY_ENGINE=v1`** — heuristic seeds, no LLM, writes to `topic_candidates`
- **`DISCOVERY_ENGINE=v2`** — Discovery Director uses LLM + Perplexity, writes to `discovery_cycles` + `opportunities`
- Both write normalized state to `discovery_state` table (single row per channel) so `/discover` page always reads from one place regardless of engine
- Approved candidates write provenance to `episode_topics` (`source='discovered'`, `discovery_score`, `candidate_id`)

### MUAPI Video Tier Routing
```
T1 = veo3-text-to-video        $2.50/gen  premium / hero scenes
T2 = veo3-fast-text-to-video   $0.60/gen  primary production tier  ← .env override
T3 = veo3-fast-text-to-video   $0.60/gen  cheap-mode alias
I2V = veo3-image-to-video                 image-to-video path
```
Controlled by `MUAPI_DEFAULT_TIER=2` in `.env`. Override individual tiers with `MUAPI_T2_MODEL`, etc. Set `MUAPI_DRY_RUN=true` to test without spending credits.

### Database Schema Migrations
New columns on existing tables go in `_V2_COLUMN_MIGRATIONS` dict in `hypogeum/db.py`. The dict is a `{table_name: [(col_name, col_def), ...]}` map. `initialize_schema()` applies them idempotently via `PRAGMA table_info()` checks.

### Cinematic Prompt Architecture
VPD scene prompts follow a strict 9-layer spec in `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md`. This file is read by the VPD agent at runtime and dictates how prompts are assembled. Key rules: no `static` camera verb (use `controlled slow push in`), always include `[no vignette]`, include `[lamp source off-frame]` when any period light source appears.

### LLM Provider Context Injected to Claude
`hypogeum/agents/scene_planner.py` contains `PROVIDER_CAPABILITIES` — a dict injected into every scene-planning LLM call. When tier or pricing changes, update this dict or the LLM will plan using stale assumptions.

---

## Test Isolation

**Never let tests touch `.hypogeum-assets/hypogeum.db`.**

Three isolation mechanisms are active in `tests/conftest.py` (all `autouse`):
1. **`.env` leakage block** — prevents production env vars from flowing into test `Config` objects
2. **OIS patching** — `is_factory_blocked()` / `is_channel_blocked()` always return `False` in tests
3. **Run registry patching** — `register_run()` / `fail_run()` are patched to avoid production DB writes

For tests that need a DB, pass `tmp_path` from pytest and call `initialize_schema(tmp_path / "test.db")`. Pass that path as `db_path=` to all DB calls.

Dashboard route tests use `server.app.config["DB_PATH"] = str(db)` and restore it after.

---

## Key `.env` Flags

| Flag | Default | Effect |
|---|---|---|
| `SMOKE_TEST_MODE` | `false` | Caps scenes/beats; forces T2 video. **Must be false for production.** |
| `BGA_ENABLED` | `false` | Runs Brand Guardian Agent (pass 1+2) before video generation |
| `MUAPI_DRY_RUN` | `false` | No real MUAPI calls; returns mock responses |
| `MUAPI_CHEAP_MODE` | `false` | Forces T3 for all video scenes |
| `MUAPI_TIMEOUT_SECONDS` | `180` | Per-job poll timeout. Veo3 Premium took 128.5s in benchmark. |
| `DISCOVERY_ENGINE` | `v1` | `v2` uses LLM + Perplexity for topic ideation (currently set to `v2` in production) |
| `EPISODE_ZERO_MAX_SCENES` | `0` | >0 enables Episode Zero mode with scene cap |

---

## Integration Contracts (never violate)

- All env access → `Config` object, never `os.getenv()`
- All logging → `hypogeum.logger.get_logger()`, never `print()` or stdlib logging
- All Claude calls → `hypogeum.agent.run_agent()` → `hypogeum.openrouter`
- All schema validation → `hypogeum.schemas.validate()`
- All DB access → `hypogeum.db.get_connection(db_path)` with explicit `db_path` in tests

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
