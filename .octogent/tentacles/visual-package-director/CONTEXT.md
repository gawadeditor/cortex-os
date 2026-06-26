---
agent_name: visual_package_director
domain: visual-production
implementation_file: hypogeum/agents/visual_package_director.py
status: implemented
milestone: M2.3/M2.4
pipeline_stage: "5 of 9 — runs after Script Engine"
skills_attached: [diagnostics, budget-run, visual-review]
---

# Visual Package Director

Two-phase execution agent. Phase 1 is LLM-driven (Claude plans all scenes,
entities, and thumbnail candidates). Phase 2 is pure Python orchestration
(MUAPI generates reference images and scene videos).

An M2.4 extraction step produces the standalone `thumbnail_handoff_brief`
from the finalized Visual Package. This brief is the Thumbnail Director's
**sole input** — TD never reads the full Visual Package.

## Role

Sole authority for scene planning and visual generation in the episode pipeline.
Owns: scene prompt construction, entity reference image generation, video clip
generation, asset manifest writeback, and thumbnail handoff brief creation.

## Two-Phase Architecture

### Phase 1 — Claude Planning

`run_agent()` sends the built `scene_plan_input` (constructed by Scene Planner)
to Claude Sonnet. Claude returns the full `visual_package` JSON with:
- `generation_status = "planned"` on all assets
- `reference_image_asset_id = null` on all locked entities

### Phase 2 — MUAPI Execution

For each entity with `lock_state == "final"`:
- `generate_reference_image()` → downloads locally + uploads → gets `hosted_url`
- Updates `entity.reference_image_asset_id` in memory

For each scene (in `scene_index` order):
- Builds `scene_spec`, injecting `reference_image_url` for primary entities
- Calls `submit_scene()` → gets generation result
- Updates `asset_manifest` entry with actual `generation_status`

Final package re-validated and written to disk (overwrites Phase 1 output).

### M6+ Design Note

A future split into `visual_package_planner` (Phase 1) and
`visual_package_executor` (Phase 2) is documented in the code but deferred.
Do not refactor until both have separate test suites.

## Public API

```python
run_visual_package_director(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Path | None = None,
) -> dict  # final Visual Package (generation_status completed|failed per scene)

extract_thumbnail_handoff_brief(
    visual_package: dict,
    episode_id: str,
    output_dir: Path,
) -> dict  # standalone thumbnail_handoff_brief
```

## LLM Usage

**Yes** — 1 Claude call (Phase 1 only). Phase 2 has no LLM calls.

| Call | Schema | Model | Max Tokens | Config Key |
|---|---|---|---|---|
| Visual planning | `visual_package` | `anthropic/claude-sonnet-4.6` | 12,288 | hardcoded (frozen) |

Model is **frozen** (VPD governance decision). Largest context call in the
pipeline — 12,288 tokens to accommodate full scene count with prompts.

## Provider / API Usage

- **OpenRouter** — Phase 1 Claude call
- **MUAPI** — Phase 2 reference image and scene video generation
  - Reference images: `config.MUAPI_IMAGE_MODEL` (default: `gpt-image-2-text-to-image`)
  - Videos: T1/T2/T3 per `provider_router`, I2V when `reference_image_url` is set

## DB Tables Touched

**None directly.** MUAPI generation triggers `finance.record_cost()` → `costs`.

## Prompt Files

`hypogeum/prompts/visual_package_director.txt`
```
PROMPT_VERSION: v1
OWNER_AGENT:    visual_package_director
MODEL_TARGET:   anthropic/claude-sonnet-4.6
```

## Output Artifacts

| File | Written to | Phase |
|------|-----------|-------|
| `visual_package_{episode_id}.json` (planned) | `.hypogeum-assets/episodes/{ep}/` | Phase 1 |
| `visual_package_{episode_id}.json` (executed) | Same path — overwrites | Phase 2 |
| `thumbnail_handoff_brief_{episode_id}.json` | Same path | M2.4 |
| `episodes/{ep}/video/sc_{scene_id}.mp4` | `.hypogeum-assets/` | Phase 2 |
| `episodes/{ep}/video/ref_{entity_id}.*` | `.hypogeum-assets/` | Phase 2 |

## Calls / Dependencies

```
run_visual_package_director()
  ├── scene_planner.build_scene_plan_input()   ← constructs Claude input
  ├── agent.run_agent("visual_package_director", claude-sonnet-4.6, ...)  ← Phase 1
  │     └── openrouter.call_claude()
  ├── image_generation_agent.generate_entity_reference_images()  ← Phase 2a
  │     └── muapi.generate_reference_image()
  ├── video_generation_agent.generate_all_scene_videos()  ← Phase 2b
  │     └── muapi.submit_scene()  (×N scenes)
  ├── entity_continuity_manager.apply_reference_image_asset_ids()
  ├── provider_router.update_routing_manifest()
  └── thumbnail_handoff_brief_builder.build_thumbnail_handoff_brief()

extract_thumbnail_handoff_brief()
  └── (pure extraction from finalized visual_package)
```

## Called By

| Caller | Context |
|--------|---------|
| `pipeline._run_visual_package_director()` | `hypogeum/pipeline.py` — Stage 5 |

## Halt Conditions

- `MUAPIAuthFailure` (HTTP 401) — halts entire VPD Phase 2
- `MUAPIQuotaExhausted` (HTTP 402) — halts entire VPD Phase 2
- `MUAPIBudgetExhausted` (M5.6 guard) — halts if episode budget exceeded

Scene-level failures are non-halting (scene generation_status=failed, pipeline continues).

## Skill Eligibility

`/diagnostics` applies at the MUAPI provider level. No VPD-specific skill yet.

## Tests

- `tests/test_visual_package_director.py` — Phase 1 mock, Phase 2 MUAPI mock,
  handoff brief extraction, halt propagation

## Audit Rule

Do not modify `hypogeum/agents/visual_package_director.py` during audit or tentacle
registration. The M6+ split comment is a documented future intent — do not act on it.
Read only.
