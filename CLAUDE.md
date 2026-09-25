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
EP → RD → NA → SE → Thumbnail Strategy → VPD → TD → Audio → Assembly → Publishing
```
1. **Executive Producer** — scores topic (ACCEPT/WARN/REJECT), produces `topic_brief`
2. **Research Director** — Perplexity queries, produces `research_package`
3. **Narrative Architect** — story structure, produces `narrative_package`
4. **Script Engine** — narration beats + visual directions, produces `script_package`
5. **Thumbnail Strategy** — three CTR concept hypotheses from `topic_brief`+`script_package`, produces `thumbnail_concept_brief` (wired into the pipeline 2026-07-19; previously reachable only via `scripts/generate_thumbnail_strategy.py`). Writing this file is what activates CONCEPT_DRIVEN_MODE in Thumbnail Director below — see `hypogeum/agents/thumbnail_strategy.py`.
6. **Visual Package Director** — plans scenes (Phase 1: Claude), generates video clips (Phase 2: MUAPI), produces `visual_package`
7. **Thumbnail Director** — generates thumbnail candidates, produces `thumbnail_package`. QA (`hypogeum/agents/thumbnail_qa.py`) requires the layered `composition_plan`/`typography_plan` fields that only CONCEPT_DRIVEN_MODE produces — legacy-mode thumbnails fail QA by design.
8. **Audio Agent** — ElevenLabs narration + music, produces `narration_package`
9. **Assembly Agent** — FFmpeg render, produces `final_render_package`
10. **Publishing Agent** — YouTube upload, produces `publishing_record`

Every agent output is validated by `hypogeum/schemas.py` (26 schemas) before passing to the next stage. A failed validation raises `SchemaError` and halts the pipeline.

### Key Modules
| Module | Role |
|---|---|
| `hypogeum/pipeline.py` | Orchestrator — calls all 10 agents in order, handles fail-fast exits |
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
T1 = veo3-text-to-video          $2.50/gen  premium / hero scenes
T2 = veo3.1-lite-text-to-video   $0.39/gen  primary production tier, 1080p candidate  ← default, .env override
T3 = veo3.1-lite-text-to-video   $0.39/gen  cheap-mode alias (same model as T2)
I2V = veo3-image-to-video                   image-to-video path
```
`veo3.1-lite-text-to-video` is the default production model for T2/T3 (confirmed 2026-07-06; better quality than `veo3-fast-text-to-video` at lower cost). Fixed 8s duration — duration/resolution are sent explicitly for veo3.1 models (see `hypogeum/muapi.py::_VEO3_1_MODELS`). Controlled by `MUAPI_DEFAULT_TIER=2` in `.env`. Override individual tiers with `MUAPI_T2_MODEL`, etc. Set `MUAPI_DRY_RUN=true` to test without spending credits.

### Video DNA v1 + Prompt Writer architecture (2026-07-14, split into per-concept DNA modules 2026-07-18, Phases B2.1–B2.6)
Canonical, topic-agnostic cinematic visual language for every video scene, now split by concept into single-owner modules per `docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md`: `hypogeum/video_dna.py` (fine-detail preservation, continuity, subject hierarchy, scene transitions), `hypogeum/camera_dna.py` (camera-verb vocabulary + static-framing policy), `hypogeum/motion_dna.py` (subject/environmental motion and pacing), `hypogeum/lighting_dna.py` (temperature/entry-verb/connector/falloff vocabulary, `[lamp source off-frame]` tag), `hypogeum/historical_dna.py` (archaeological/period-plausibility rule, material-observation pattern, per-setting-type anachronism library), `hypogeum/composition_dna.py` (spatial/frame-layout structure — optional section, not yet default-on, see that module's docstring), and `hypogeum/forbidden_dna.py` (aggregation/dedup of every module's negative items — the sole collector, not a content source). Every scene prompt is assembled by `hypogeum/video_prompt_writer.py::assemble_video_scene_prompt()` (the only thing `hypogeum/muapi.py::submit_scene()` calls) in a fixed section order — **Scene → Camera → Subject → Motion → Lighting → Visual Priorities → [Composition, optional] → Video DNA → Camera DNA → Motion DNA → Lighting DNA → Historical DNA → Brand DNA → Forbidden** — never mixed together; see that module's own docstring for the authoritative order and which sections are default-on vs. optional. Each concept has exactly one owner across these modules; `hypogeum.brand.visual_identity`'s adaptive-tier directive owns material character, finish, lighting *character* (color-temperature philosophy, distinct from Lighting DNA's technical vocabulary), and color philosophy only. All prohibitions live in one dedicated Forbidden section (collected + deduplicated by `hypogeum/forbidden_dna.py` from every module) — never scattered "never/no/avoid" language. A deterministic compression pass removes duplicate concept restatements across the deterministic sections (never touches Scene/Camera/Subject/Motion/Lighting). Never edit a scene prompt by hand to add cinematic guidance — extend the relevant DNA module instead so every future episode inherits the change automatically. See `hypogeum/video_dna_validation_suite.py` (18 topic-agnostic validation scenes) + `hypogeum/video_dna_validator.py` (structural, zero-provider-call checker, supports comparing Video DNA versions) for the reusable regression suite.

### Database Schema Migrations
New columns on existing tables go in `_V2_COLUMN_MIGRATIONS` dict in `hypogeum/db.py`. The dict is a `{table_name: [(col_name, col_def), ...]}` map. `initialize_schema()` applies them idempotently via `PRAGMA table_info()` checks.

### Cinematic Prompt Architecture
VPD scene prompts follow a strict 9-layer spec in `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md`. This file is read by the VPD agent at runtime and dictates how prompts are assembled. Key rules: static framing is constrained, not banned — permitted only with meaningful subject/environmental/atmospheric/lighting motion in frame and only when intentional, rare, and narratively justified (default to `controlled slow push in` otherwise); this canonical static-framing policy is owned by `hypogeum/camera_dna.py` (as of Phase B2.5), not restated here (see `docs/validation/prompt_writer_architecture/B1_2A_STATIC_CAMERA_POLICY_MEMO.md`); always include `[no vignette]`; include `[lamp source off-frame]` when any period light source appears.

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

## Verifying Published Assets (thumbnails, titles, anything "live")

**Never diagnose or fix a live YouTube/social asset by pattern-matching a plausible-sounding heuristic** (file-size, CDN slot number/naming like `oar1`/`oar2`/`oar3`, "the last one", "the biggest one"). On 2026-09-25 this caused two full wrong remediation passes across ~30 videos before the real cause was found — see `memory/project_youtube_legacy_thumbnail_gap_20260925.md` (or the equivalent memory in whatever memory store is active) for the full incident.

The real source of truth for a video's designed thumbnail, in the order to check:
1. The dashboard's Content Grid (`/channel/<id>/grid`) — each card's own poster image (`media.zernio.com/.../_main_episode.jpg` or `_short_00N.jpg`), unless flagged "Cover confirmed uploaded by hand".
2. If hand-flagged: `/manual-cover/<episode_id>[_<short_id>].jpg` on the dashboard — but **visually confirm it against the video's own title/description before trusting it**; this mapping has been wrong at least once.
3. The episode's own archive folder — `/Volumes/Archive/Cortex/<channel>/<episode_id>*/thumbnails/` — ground truth regardless of any dashboard mapping. Find the episode_id by matching a distinctive phrase from the video's real description against archived `topic_brief_*.json` / caption `.srt` files.
4. A handful of the oldest hypogeum Shorts predate this factory (produced by an earlier separate project). Check `~/Desktop/hypogeum_legacy_covers/` and `~/faceless-channel/output/` if nothing above exists.

Before applying any fix across more than one file, verify it on a single example and confirm the match is real (visually, against the actual title/description) — never generalize a pattern from one or two samples to a whole batch without checking each one.

The only real fix once you have the right file: re-upload it through YouTube Studio's own Thumbnail control on the video's edit page, then Save. No code path in this repo can fix a wrong live thumbnail — this is entirely downstream of Zernio/YouTube's own systems.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
