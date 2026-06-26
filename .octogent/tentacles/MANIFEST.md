# Hypogeum Tentacle Manifest

**Last updated:** 2026-06-26 (Phase 3C — Skills Attachment + Prompt Registry + Discovery v2)

Maps every `hypogeum/agents/` and `hypogeum/discovery/` Python module to its
OctoGent tentacle registration.

Status values: `✅ registered` | `✅ Phase 3A` | `✅ Phase 3B` | `✅ Phase 3C` | `⚠ unwired` | `❌ unregistered`

---

## 1. Core Pipeline Agents (9-stage episode pipeline)

| Pipeline Stage | Python Module | Tentacle Directory | Status |
|---|---|---|---|
| 1 — Executive Producer | `agents/executive_producer.py` | `executive-producer/` | ✅ Phase 3B |
| 2 — Research Director | `agents/research_director.py` | `research-director/` | ✅ Phase 3B |
| 3 — Narrative Architect | `agents/narrative_architect.py` | `narrative-architect/` | ✅ Phase 3B |
| 4 — Script Engine | `agents/script_engine.py` | `script-engine/` | ✅ Phase 3B |
| 5 — Visual Package Director | `agents/visual_package_director.py` | `visual-package-director/` | ✅ Phase 3B |
| 6 — Thumbnail Director | `agents/thumbnail_director.py` | `thumbnail-director/` | ✅ Phase 3B |
| 7 — Audio Agent | `agents/audio_agent.py` | `audio-agent/` | ✅ Phase 3B |
| 8 — Assembly Agent | `agents/assembly_agent.py` | `assembly-agent/` | ✅ Phase 3B |
| 9 — Publishing Agent | `agents/publishing_agent.py` | `publishing-agent/` | ✅ Phase 3B |

---

## 2. Post-Pipeline Agents (non-blocking, run after Publishing Agent)

| Agent | Python Module | Tentacle Directory | Status |
|---|---|---|---|
| Archive Agent (R49) | `agents/archive_agent.py` | `archive-agent/` | ✅ |
| Compliance Agent (R48) | `agents/compliance_agent.py` | `compliance-agent/` | ✅ |
| Finance Agent (R50) | `agents/finance_agent.py` | `finance-agent/` | ✅ |
| QA Agent (R47) | `agents/qa_agent.py` | `qa-agent/` | ✅ |
| Asset Archive Agent | `agents/asset_archive_agent.py` | `asset-archive-agent/` | ✅ Phase 3A |

---

## 3. Visual Production Sub-Agents (called by VPD / Thumbnail Director)

| Agent | Python Module | Tentacle Directory | Status |
|---|---|---|---|
| Entity Continuity Manager | `agents/entity_continuity_manager.py` | `entity-continuity-manager/` | ✅ |
| Image Generation Agent | `agents/image_generation_agent.py` | `image-generation-agent/` | ✅ |
| Provider Router | `agents/provider_router.py` | `provider-router/` | ✅ |
| Scene Planner | `agents/scene_planner.py` | `scene-planner/` | ✅ |
| Video Generation Agent | `agents/video_generation_agent.py` | `video-generation-agent/` | ✅ |
| Thumbnail Generation Agent | `agents/thumbnail_generation_agent.py` | `thumbnail-generation-agent/` | ✅ |
| Thumbnail Handoff Brief Builder | `agents/thumbnail_handoff_brief_builder.py` | `thumbnail-handoff-brief-builder/` | ✅ |
| Thumbnail Identity Guardian | `agents/thumbnail_identity_guardian.py` | `thumbnail-identity-guardian/` | ✅ |
| Thumbnail Prompt Writer | `agents/thumbnail_prompt_writer.py` | `thumbnail-prompt-writer/` | ✅ |
| Thumbnail Composition Planner | `agents/thumbnail_composition_planner.py` | `thumbnail-composition-planner/` | ✅ Phase 3A |
| Thumbnail Typography Planner | `agents/thumbnail_typography_planner.py` | `thumbnail-typography-planner/` | ✅ Phase 3A |
| Thumbnail Prompt Validator | `agents/thumbnail_prompt_validator.py` | `thumbnail-prompt-validator/` | ✅ Phase 3A |
| Thumbnail Plan Validator | `agents/thumbnail_plan_validator.py` | `thumbnail-plan-validator/` | ✅ Phase 3A |
| Thumbnail Killer Concept Validator | `agents/thumbnail_killer_concept_validator.py` | `thumbnail-killer-concept-validator/` | ✅ Phase 3A |
| Thumbnail QA | `agents/thumbnail_qa.py` | `thumbnail-qa/` | ✅ Phase 3A |
| Thumbnail Compositor | `agents/thumbnail_compositor.py` | `thumbnail-compositor/` | ✅ Phase 3A |
| Reference Image QA | `agents/reference_image_qa.py` | `reference-image-qa/` | ✅ Phase 3A — ⚠ UNWIRED |

---

## 4. Assembly Sub-Agents (called by Assembly Agent)

| Agent | Python Module | Tentacle Directory | Status |
|---|---|---|---|
| Video Renderer | `agents/video_renderer.py` | `video-renderer/` | ✅ |
| Timeline Builder | `agents/timeline_builder.py` | `timeline-builder/` | ✅ |
| Final Render Reviewer | `agents/final_render_reviewer.py` | `final-render-reviewer/` | ✅ |
| Media Specs QA | `agents/media_specs_qa.py` | `media-specs-qa/` | ✅ |
| Timeline QA | `agents/timeline_qa.py` | `timeline-qa/` | ✅ Phase 3A |

---

## 5. Distribution / Audio Sub-Agents (called by Audio Agent)

| Agent | Python Module | Tentacle Directory | Status |
|---|---|---|---|
| Voice Generation Operator | `agents/voice_generation_operator.py` | `voice-generation-operator/` | ✅ |
| Audio QA | `agents/audio_qa.py` | `audio-qa/` | ✅ |
| Caption Agent | `agents/caption_agent.py` | `caption-agent/` | ✅ |
| Music Agent | `agents/music_agent.py` | `music-agent/` | ✅ |
| Brand Guardian Agent | `agents/brand_guardian_agent.py` | `brand-guardian/` | ✅ |
| Covered Topics Writer | `agents/covered_topics_writer.py` | `covered-topics-writer/` | ✅ |

---

## 6. Discovery Sub-Agents (called by Discovery Director)

| Agent | Python Module | Tentacle Directory | Status |
|---|---|---|---|
| Discovery Director | `agents/discovery_director.py` | `discovery-director/` | ✅ Phase 3A (tentacle); implemented Phase 3C |
| Topic Ideator | `discovery/topic_ideator.py` | `topic-ideator/` | ✅ Phase 3B |
| Source Verifier | `discovery/source_verifier.py` | `source-verifier/` | ✅ Phase 3B |
| Queue Gateway | `discovery/queue_gateway.py` | `queue-gateway/` | ✅ Phase 3B |
| Discovery Repository | `discovery/repository.py` | `discovery-repository/` | ✅ Phase 3B |

---

## 7. Category Tentacles (domain grouping, not agent-specific)

| Tentacle | Domain | Purpose |
|---|---|---|
| `distribution/` | Distribution | Audio/post-production agent grouping with per-agent spec files |
| `editorial-pipeline/` | Editorial | Content generation chain context + covered-topics memory |
| `visual-production/` | Visual | Visual generation + thumbnail sub-pipeline context |
| `provider-integrations/` | Providers | Integration specs for all external APIs |

---

## 8. Coverage Summary

```
Total hypogeum/agents/ Python modules (excl. __init__):   43
Total hypogeum/discovery/ sub-modules registered:          4
                                                        ────
Total Python modules in scope:                            47

Registered (own CONTEXT.md):                             47
  — Pre-Phase-3 registrations:                           23  (core sub-agents, post-pipeline)
  — Phase 3A additions:                                  11  (discovery-director + thumbnail sub-pipeline + timeline-qa + reference-image-qa)
  — Phase 3B additions:                                  13  (9 pipeline stages + 4 discovery sub-modules)

Unregistered after Phase 3C:                              0  ← ALL ACTIVE MODULES REGISTERED
Unwired-but-registered:                                   1  (reference-image-qa — wiring pending)
Category tentacles (no Python agent):                     4  (distribution, editorial-pipeline, visual-production, provider-integrations)

Phase 3C completion (2026-06-26):
  skills_attached: applied to 13 agent CONTEXT.md files (see §12)
  prompt registry: prompt_loader.py + prompt_registry.py introduced;
    all 12 prompt .txt files now carry PROMPT_VERSION headers (see §15)
  Discovery v2: discovery_director.py + discovery/ package committed;
    opportunities schema + initial_scorer.py added (see §14)
```

---

## 9. Remaining Gaps (post-Phase-3C)

**All active production Python modules now have tentacles.**

| Item | Description | Blocks episode run? |
|---|---|---|
| ~~Skills attachment~~ | ~~`skills_attached:` fields on all CONTEXT.md files~~ — **RESOLVED Phase 3C 2026-06-26** | — |
| ~~Source verifier inline query template~~ | ~~Not yet in prompt registry~~ — **RESOLVED Phase 2A follow-up 2026-06-26**: `source_verifier_query.txt` created; `_VERIFY_QUERY_TEMPLATE` now loaded via `load_prompt()`; registered in `/api/prompts` | — |
| ~~AIVA integration spec~~ | ~~`provider-integrations/aiva-integration.md` is an archived spec~~ — **RESOLVED 2026-06-01**: file marked VOIDED; ElevenLabs Music is sole AI provider; see `elevenlabs-music-integration.md §13` | — |
| Reference image QA wiring | `reference_image_qa` implemented but not called from VPD Phase 2. Module is outside the current production call graph. | **No** — module is not in the production call path |
| `publishing/connectors.py` stubs | `PlatformConnector.publish()` raises `NotImplementedError`. Publishing agent calls the platform-specific clients (`youtube.py`, `meta.py`, `tiktok.py`) directly; `connectors.py` is not in the production call path. | **No** — dead code not invoked at runtime |

---

## 10. Duplicate / Overlap Notes

No duplicates introduced by Phase 3B or Phase 3C. Verified:

- `archive-agent/` → `agents/archive_agent.py` (publication archive R49)
- `asset-archive-agent/` → `agents/asset_archive_agent.py` (JSONL asset catalog)
  — **distinct responsibilities**, separate tentacles confirmed.

- `thumbnail-director/` (Phase 3B) covers `agents/thumbnail_director.py`
  — does **not** overlap with `thumbnail-prompt-writer/` (which covers the
  prompt-writing sub-agent called *by* the director).

- `visual-package-director/` (Phase 3B) covers the full VPD orchestrator.
  Its sub-agents each have their own tentacles:
  `entity-continuity-manager`, `image-generation-agent`, `provider-router`,
  `scene-planner`, `video-generation-agent` — no overlap.

---

## 11. Backend Module → Tentacle Index (full alphabetical)

| Backend Module | Tentacle Directory | LLM? | Provider |
|---|---|---|---|
| `agents/archive_agent.py` | `archive-agent/` | No | None |
| `agents/assembly_agent.py` | `assembly-agent/` | **No** | ElevenLabs, ffmpeg |
| `agents/asset_archive_agent.py` | `asset-archive-agent/` | No | None |
| `agents/audio_agent.py` | `audio-agent/` | **No** | ElevenLabs (via VGO) |
| `agents/audio_qa.py` | `audio-qa/` | No | None |
| `agents/brand_guardian_agent.py` | `brand-guardian/` | **Yes** | OpenRouter (2 passes) |
| `agents/caption_agent.py` | `caption-agent/` | No | ElevenLabs (alignment) |
| `agents/compliance_agent.py` | `compliance-agent/` | No | None |
| `agents/covered_topics_writer.py` | `covered-topics-writer/` | No | None |
| `agents/discovery_director.py` | `discovery-director/` | No (delegates) | OR + Perplexity (via sub) |
| `agents/entity_continuity_manager.py` | `entity-continuity-manager/` | No | None |
| `agents/executive_producer.py` | `executive-producer/` | **Yes** | OpenRouter (2×) |
| `agents/final_render_reviewer.py` | `final-render-reviewer/` | No | None |
| `agents/finance_agent.py` | `finance-agent/` | No | None |
| `agents/image_generation_agent.py` | `image-generation-agent/` | No | MUAPI |
| `agents/media_specs_qa.py` | `media-specs-qa/` | No | None (PIL) |
| `agents/music_agent.py` | `music-agent/` | No | ElevenLabs |
| `agents/narrative_architect.py` | `narrative-architect/` | **Yes** | OpenRouter (Opus) |
| `agents/provider_router.py` | `provider-router/` | No | None |
| `agents/publishing_agent.py` | `publishing-agent/` | **No** | YouTube, Meta, TikTok |
| `agents/qa_agent.py` | `qa-agent/` | No | None |
| `agents/reference_image_qa.py` | `reference-image-qa/` | No | PIL — ⚠ UNWIRED |
| `agents/research_director.py` | `research-director/` | **Yes** | OpenRouter (Opus) + Perplexity |
| `agents/scene_planner.py` | `scene-planner/` | No | None |
| `agents/script_engine.py` | `script-engine/` | **Yes** | OpenRouter (Sonnet) |
| `agents/thumbnail_composition_planner.py` | `thumbnail-composition-planner/` | No | None |
| `agents/thumbnail_compositor.py` | `thumbnail-compositor/` | No | MUAPI, PIL |
| `agents/thumbnail_director.py` | `thumbnail-director/` | **Yes** | OpenRouter (GPT planning via `TD_PLANNING_MODEL`); vision gate delegated to TIG (separate agent) |
| `agents/thumbnail_generation_agent.py` | `thumbnail-generation-agent/` | No | MUAPI |
| `agents/thumbnail_handoff_brief_builder.py` | `thumbnail-handoff-brief-builder/` | No | None |
| `agents/thumbnail_identity_guardian.py` | `thumbnail-identity-guardian/` | **Yes** | OpenRouter (vision) |
| `agents/thumbnail_killer_concept_validator.py` | `thumbnail-killer-concept-validator/` | No | None |
| `agents/thumbnail_plan_validator.py` | `thumbnail-plan-validator/` | No | None |
| `agents/thumbnail_prompt_validator.py` | `thumbnail-prompt-validator/` | No | None |
| `agents/thumbnail_prompt_writer.py` | `thumbnail-prompt-writer/` | **Yes** | OpenRouter (via run_agent) |
| `agents/thumbnail_qa.py` | `thumbnail-qa/` | No | PIL |
| `agents/thumbnail_typography_planner.py` | `thumbnail-typography-planner/` | No | None |
| `agents/timeline_builder.py` | `timeline-builder/` | No | None |
| `agents/timeline_qa.py` | `timeline-qa/` | No | ffprobe |
| `agents/video_generation_agent.py` | `video-generation-agent/` | No | MUAPI |
| `agents/video_renderer.py` | `video-renderer/` | No | ffmpeg |
| `agents/visual_package_director.py` | `visual-package-director/` | **Yes** | OpenRouter (Sonnet) + MUAPI |
| `agents/voice_generation_operator.py` | `voice-generation-operator/` | No | ElevenLabs |
| `discovery/queue_gateway.py` | `queue-gateway/` | No | None |
| `discovery/repository.py` | `discovery-repository/` | No | SQLite |
| `discovery/source_verifier.py` | `source-verifier/` | No | Perplexity |
| `discovery/topic_ideator.py` | `topic-ideator/` | **Yes** | OpenRouter (Sonnet) |

---

## 12. Skill Attachment Map (Phase 3C — applied 2026-06-26)

Skills are attached to CONTEXT.md files via `skills_attached:` in frontmatter.
Only local skills (`.claude/skills/`) are attached. GitHub skills deferred — see §13.

| Skill | Attached Tentacles | Local file |
|---|---|---|
| `/collect` | `asset-archive-agent` | `.claude/skills/collect.md` |
| `/visual-review` | `thumbnail-qa`, `timeline-qa`, `final-render-reviewer` | `.claude/skills/visual-review.md` |
| `/diagnostics` | `executive-producer`, `research-director`, `visual-package-director`, `thumbnail-director`, `topic-ideator`, `source-verifier` | `.claude/skills/diagnostics.md` |
| `/bga-preflight` | `brand-guardian` | `.claude/skills/bga-preflight.md` |
| `/brand-extract` | `brand-guardian` | `.claude/skills/brand-extract.md` |
| `/budget-run` | `finance-agent`, `assembly-agent`, `visual-package-director` | `.claude/skills/budget-run.md` |
| `/cost-forensic` | `finance-agent` | `.claude/skills/cost-forensic.md` |

**Pipeline-level skill (not attached to any single tentacle):**

| Skill | Scope | Local file |
|---|---|---|
| `/ep0-run` | Invoke before any 9-stage episode run. Operates at pipeline entry — no single agent owns it. | `.claude/skills/ep0-run.md` |

**Agents intentionally without skills (deterministic / non-LLM):**

| Tentacle | Reason |
|---|---|
| `archive-agent` | Deterministic archivist; no operator judgment needed pre-run |
| `audio-agent` | Pure ElevenLabs delegation; no pre-run diagnosis surface |
| `audio-qa` | Deterministic checks; no operator-invocable review skill exists |
| `caption-agent` | Deterministic; no pre-run operator gate |
| `compliance-agent` | Deterministic; no pre-run operator gate |
| `covered-topics-writer` | Deterministic writer; no operator surface |
| `discovery-director` | Delegates to sub-modules; sub-modules carry `/diagnostics` |
| `discovery-repository` | Pure SQLite reads/writes; no operator surface |
| `entity-continuity-manager` | Deterministic lookup; no operator surface |
| `image-generation-agent` | MUAPI caller; cost governed via VPD + `/budget-run` |
| `media-specs-qa` | Deterministic PIL checks; no operator surface |
| `music-agent` | ElevenLabs caller; no operator pre-run gate |
| `narrative-architect` | LLM agent, but no applicable local skill (no narration skill exists yet) |
| `provider-router` | Deterministic routing; no operator surface |
| `publishing-agent` | Platform publishing; no applicable local skill |
| `qa-agent` | Deterministic gate; no operator surface |
| `queue-gateway` | Deterministic queue ops; no operator surface |
| `reference-image-qa` | UNWIRED + deterministic PIL; no operator surface |
| `scene-planner` | Deterministic; no operator surface |
| `script-engine` | LLM agent, but no applicable local skill (no script review skill exists yet) |
| `thumbnail-compositor` | Deterministic PIL+MUAPI; no operator surface |
| `thumbnail-composition-planner` | Deterministic; no operator surface |
| `thumbnail-generation-agent` | MUAPI caller; cost governed via TD + `/budget-run` on VPD |
| `thumbnail-handoff-brief-builder` | Deterministic builder; no operator surface |
| `thumbnail-identity-guardian` | Vision gate; operates post-generation (no pre-run operator skill maps to it) |
| `thumbnail-killer-concept-validator` | Deterministic; no operator surface |
| `thumbnail-plan-validator` | Deterministic; no operator surface |
| `thumbnail-prompt-validator` | Deterministic; no operator surface |
| `thumbnail-prompt-writer` | LLM sub-agent; governed by TD; no standalone pre-run skill |
| `thumbnail-typography-planner` | Deterministic; no operator surface |
| `timeline-builder` | Deterministic; no operator surface |
| `video-generation-agent` | MUAPI caller; cost governed via VPD + `/budget-run` |
| `video-renderer` | ffmpeg wrapper; no operator surface |
| `voice-generation-operator` | ElevenLabs caller; no operator surface |

**Category tentacles (no agent-level skills):** `distribution/`, `editorial-pipeline/`, `visual-production/`, `provider-integrations/`

---

## 13. Operator UX Skills — Installed Locally, Not Agent-Attached

The following skills are installed locally in `.claude/skills/` and are
available to the operator. They are not attached to any agent tentacle because
they address dashboard and UX concerns, not production pipeline agents.

| Skill file | Purpose |
|---|---|
| `analytics_experience_design.md` | Operator UX — dashboard analytics design |
| `dashboard_ux.md` | Operator UX — dashboard layout and interaction |
| `data_visualization.md` | Operator UX — chart and data display |
| `frontend_polish.md` | Operator UX — frontend quality and polish |
| `information_architecture.md` | Operator UX — navigation and IA |
| `ui_ux_audit.md` | Operator UX — audit of operator-facing UI |

---

## 14. Infrastructure Modules Without Tentacles

These Python modules live in `hypogeum/` but are **not agents** — they are
pure library/infrastructure code called by agents or the dashboard. They do
not require OctoGent tentacle registration because they have no independent
operator surface, no system prompt, and no direct invocation by the harness.

| Module | Added | Role | Caller(s) |
|---|---|---|---|
| `hypogeum/initial_scorer.py` | Phase 3C / Discovery v2 | Deterministic structural scorer: computes `initial_composite` for opportunity records. Pure Python — no LLM, no DB, no network. Sole owner of `opportunities.initial_composite` (schema contract in `schema.sql`). | `discovery/topic_ideator.py` (current sole runtime caller) |
| `hypogeum/command_state.py` | Phase F0 | Read-only operational snapshot aggregator for the Command Center dashboard widget. Derives factory health, provider status, run state, and attention queue from real DB rows — no external calls. | `run_dashboard.py` (dashboard route) |

**Classification rule:** Every `hypogeum/agents/*.py` and `hypogeum/discovery/*.py`
module has a tentacle, regardless of whether it uses an LLM. Modules in
`hypogeum/` root, `hypogeum/skills/`, `hypogeum/publishing/`, or other
subdirectories do not have tentacles unless they are explicitly registered
as agents.

---

## 15. Prompt Registry (Phase 2A/2B — applied 2026-06-26)

All LLM system prompt files now carry a version header block at the top of each
`.txt` file. The header is stripped at load time by `prompt_loader.load_prompt()`
before the text is returned to the agent. The registry is queryable via
`prompt_registry.list_prompts()` / `prompt_registry.get_prompt(name)` and exposed
at `GET /api/prompts` and `GET /api/prompts/<name>`.

### Header format (in every prompt .txt file)

```
# PROMPT_VERSION: v1
# OWNER_AGENT: <agent_name>
# MODEL_TARGET: <provider/model-slug>
# PURPOSE: <one-line description>
```

### Registered prompt files (11 total)

| Prompt file | Loaded by | Tentacle |
|---|---|---|
| `brand_guardian_agent.txt` | `agents/brand_guardian_agent.py` | `brand-guardian/` |
| `executive_producer.txt` | `agents/executive_producer.py` | `executive-producer/` |
| `narrative_architect.txt` | `agents/narrative_architect.py` | `narrative-architect/` |
| `perplexity_research.txt` | `hypogeum/perplexity.py` (infrastructure) | — (no tentacle) |
| `research_director.txt` | `agents/research_director.py` | `research-director/` |
| `script_engine.txt` | `agents/script_engine.py` | `script-engine/` |
| `source_verifier_query.txt` | `discovery/source_verifier.py` | `source-verifier/` |
| `thumbnail_director.txt` | `agents/thumbnail_prompt_writer.py` (AGENT_NAME="thumbnail_director") | `thumbnail-prompt-writer/` |
| `thumbnail_identity_guardian.txt` | `agents/thumbnail_identity_guardian.py` | `thumbnail-identity-guardian/` |
| `topic_ideator.txt` | `discovery/topic_ideator.py` | `topic-ideator/` |
| `visual_package_director.txt` | `agents/visual_package_director.py` | `visual-package-director/` |

**Notes:**
- `thumbnail_director.txt` is loaded by `thumbnail_prompt_writer.py`, not by `thumbnail_director.py`. The name reflects the prompt's origin (extracted from TD's planning phase); `_AGENT_NAME = "thumbnail_director"` is preserved for registry identity.
- `perplexity_research.txt` is loaded by `hypogeum/perplexity.py` (an infrastructure client, not an agent). It has no tentacle.
- `thumbnail_strategy.txt` is loaded by `hypogeum/skills/thumbnail_strategy.py` (an operator-invoked skill, outside the `agents/` + `discovery/` scope of this MANIFEST). See §U note below.
- `music_categories.json` in `hypogeum/prompts/` is a data file (not a `.txt` prompt), loaded by `elevenlabs.py` via `prompt_loader.load_music_categories()`. It is not registered in the prompt registry and does not carry version headers.

**§U — Prompt files outside agent scope (not in table above):**

| Prompt file | Loaded by | Why excluded |
|---|---|---|
| `thumbnail_strategy.txt` | `hypogeum/skills/thumbnail_strategy.py` | Operator-invoked skill; outside `agents/` + `discovery/` scope |

### Modules that call `load_prompt()`

The following modules call `prompt_loader.load_prompt(path)` to strip version
headers before use. Pipeline stages 6–9 (Thumbnail Director, Audio, Assembly,
Publishing) are non-LLM or delegate prompt loading to sub-agents and do not
call `load_prompt()` directly.

**Python agent files (`hypogeum/agents/`):**
`executive_producer.py`, `narrative_architect.py`, `research_director.py`,
`script_engine.py`, `visual_package_director.py`, `brand_guardian_agent.py`,
`thumbnail_identity_guardian.py`, `thumbnail_prompt_writer.py`

**Infrastructure (`hypogeum/`):**
`perplexity.py`

**Discovery sub-agents (`hypogeum/discovery/`):**
`topic_ideator.py`, `source_verifier.py`

**Skills (`hypogeum/skills/`):**
`thumbnail_strategy.py`
