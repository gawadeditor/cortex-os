# B0 — Skill Wiring Fix Report

**Date:** 2026-07-08
**Source:** docs/architecture/SKILL_LOADING_AUDIT.md (A0)
**Status:** COMPLETE

---

## Objective

Fix the skill wiring problems identified in the A0 audit:
- M-02: Provider translation skills authored but injected into no agent
- m-01: `thumbnail_intelligence_pipeline.md` injected into no agent
- m-02: Shot Diversity Planner operating without the Visual Language Engine
- i-02: Missing skill files produced silent empty strings with no warning

---

## Files Changed

### New

| File | Purpose |
|---|---|
| `hypogeum/skill_registry.py` | Centralized skill registry. All 7 internal skills registered with path, type, status, wired_to list, and rationale. Provides `log_loaded_skills(agent, skill_paths, log)` diagnostic helper. |

### Modified

| File | What Changed |
|---|---|
| `hypogeum/agents/visual_package_director.py` | Added `_PROVIDER_SKILLS_DIR`, `_VEO_TRANSLATION_SKILL_FILE`, `_SEEDANCE_TRANSLATION_SKILL_FILE`, `_VPD_ALL_SKILL_PATHS` constants. Wired both provider skills into `_load_scene_prompts_prompt()` (Stage B). Added `from hypogeum import skill_registry`. Added `_skill_registry.log_loaded_skills()` call at the top of `run_visual_package_director()`. |
| `hypogeum/agents/shot_diversity_planner.py` | Added `_VI_SKILLS_DIR`, `_VLE_SKILL_FILE`, `_SDP_ALL_SKILL_PATHS` constants. Changed `_load_prompt()` to call `compose_system_prompt(_PROMPT_FILE, _VLE_SKILL_FILE)`. Added `from hypogeum import skill_registry`. Added `_skill_registry.log_loaded_skills()` call at the top of `run_shot_diversity_planner()`. |
| `hypogeum/agents/thumbnail_prompt_writer.py` | Replaced `load_prompt` import with `compose_system_prompt`. Added `_VI_SKILLS_DIR`, `_THUMBNAIL_INTELLIGENCE_SKILL_FILE`, `_TPW_ALL_SKILL_PATHS` constants. Changed `_load_system_prompt()` to call `compose_system_prompt(_PROMPT_FILE, _THUMBNAIL_INTELLIGENCE_SKILL_FILE)`. Added `from hypogeum import skill_registry`. Added `_skill_registry.log_loaded_skills()` call inside `write_thumbnail_prompts()`. |

---

## Agents Affected

### `visual_package_director` (Stage B — Scene Prompt Writing)

**Before B0:** Stage B composed `vpd_scene_prompts.txt` + `cinematic_prompt_architecture.md` only.

**After B0:** Stage B composes `vpd_scene_prompts.txt` + `cinematic_prompt_architecture.md` + `veo_translation_skill.md` + `seedance_translation_skill.md`.

The two provider translation skills teach the Stage B LLM how Veo3 and Seedance 2.0 each interpret prompt vocabulary. Stage B writes the actual `primary_prompt` and `fallback_prompt` strings that are sent to MUAPI. Having provider vocabulary context at prompt-writing time means the output language matches what each provider reliably acts on.

All other VPD stages are unchanged:
- Stage A1 (metadata): no skills — no change
- Stage A2 (scene skeleton batches): no skills — no change
- Stage A4 (presentation planner): `visual_language_engine.md` — no change
- Visual Intent QA: no skills — no change

### `shot_diversity_planner` (Shot Diversity Planning)

**Before B0:** `compose_system_prompt(_PROMPT_FILE)` — no skills. The SDP assigned `shot_scale`, `camera_movement`, `camera_angle`, `composition`, `visual_motif` to every scene using vocabulary defined in the Visual Language Engine, but without that document in its system prompt.

**After B0:** `compose_system_prompt(_PROMPT_FILE, _VLE_SKILL_FILE)` — VLE is now active.

The Visual Language Engine defines the exact vocabulary set the SDP is supposed to use (presentation_method categories, shot scale names, camera movement types). Without it, the SDP was relying on its base prompt and training data to replicate that vocabulary from scratch.

### `thumbnail_prompt_writer` (Thumbnail Phase 1 — LLM Planning)

**Before B0:** `load_prompt(_PROMPT_FILE)` — base prompt only.

**After B0:** `compose_system_prompt(_PROMPT_FILE, _THUMBNAIL_INTELLIGENCE_SKILL_FILE)` — thumbnail intelligence pipeline is now active.

The `thumbnail_intelligence_pipeline.md` describes the authoritative execution sequence for the Thumbnail Director (CTR Strategy → Composition → Typography → TIG → QA → MUAPI Generation → Final Selection). Injecting this into the planning LLM gives it awareness of the downstream stages it is writing prompts for.

---

## Skills Now Loaded

| Skill | Status Before B0 | Status After B0 | Agent | Stage |
|---|---|---|---|---|
| `cinematic_prompt_architecture.md` | Wired | Wired (no change) | VPD | Stage A legacy + Stage B |
| `visual_language_engine.md` | Wired (VPD A4 only) | Wired + SDP added | VPD (A4) + SDP | Presentation planning + Diversity planning |
| `veo_translation_skill.md` | **UNWIRED** | Wired | VPD | Stage B (scene prompts) |
| `seedance_translation_skill.md` | **UNWIRED** | Wired | VPD | Stage B (scene prompts) |
| `thumbnail_intelligence_pipeline.md` | **UNWIRED** | Wired | thumbnail_prompt_writer | Phase 1 planning |

---

## Skills Still Unused (Intentional)

| Skill | Status | Reason |
|---|---|---|
| `provider_skill_template.md` | GOVERNANCE | Meta-document defining the structure requirements for provider skills. Used by humans when authoring new provider skills. Contains no provider behavioral information — injecting it into agents would add noise without benefit. |
| `veo_prompt_skill.md` | SUPERSEDED | Veo prompt translation v1.0. Superseded by `veo_translation_skill.md` v3.0. Not injected. Retained for historical reference only. |

Both are registered in `skill_registry.py` with explicit status and rationale. Neither is "silently unused."

---

## Diagnostic Log

Each affected agent now emits a `skill_manifest` structured log event at the start of every run, via `skill_registry.log_loaded_skills()`:

```jsonl
{"event": "skill_manifest", "agent": "visual_package_director", "loaded": ["cinematic_prompt_architecture.md", "visual_language_engine.md", "veo_translation_skill.md", "seedance_translation_skill.md"], "missing": [], "loaded_count": 4, "missing_count": 0, ...}
{"event": "skill_manifest", "agent": "shot_diversity_planner", "loaded": ["visual_language_engine.md"], "missing": [], "loaded_count": 1, "missing_count": 0, ...}
{"event": "skill_manifest", "agent": "thumbnail_director", "loaded": ["thumbnail_intelligence_pipeline.md"], "missing": [], "loaded_count": 1, "missing_count": 0, ...}
```

If any skill file is missing on disk, a `skill_files_missing` warn event fires alongside `skill_manifest`. This replaces the silent empty-string degradation that was the previous behavior (audit finding i-02).

---

## Risks

### R-01: Stage B system prompt growth (LOW)

**What changed:** `SCENE_PROMPTS_PROMPT` in VPD grew from ~40KB to ~140KB by adding both provider translation skills.

**Risk:** Larger system prompts consume more tokens and increase latency and cost. Provider translation skills each contain detailed behavioral documentation. However:
- Both skills are reference documents (no executable content)
- The Stage B prompt is already large (cinematic_prompt_architecture.md is substantial)
- OpenRouter/Claude handles large system prompts without truncation at this scale

**Mitigation if needed:** Consider splitting Stage B into provider-specific calls (one call for Veo-routed scenes, one for Seedance-routed scenes) so only the relevant provider skill is present. This is NOT done in B0 — it would require routing changes outside the skill wiring scope.

### R-02: SDP vocabulary expansion (LOW)

**What changed:** The Shot Diversity Planner now has the full Visual Language Engine vocabulary in its system prompt.

**Risk:** The VLE is comprehensive. Adding it could cause the SDP to over-constrain assignments or select vocabulary values that don't match the SDP's schema. The SDP output schema validates the specific field values expected — if VLE introduces vocabulary the schema doesn't recognize, schema validation will catch it.

**Evidence that risk is low:** The SDP schema and the VLE vocabulary were designed together. VLE defines the source vocabulary that the SDP schema was written to accept.

### R-03: Thumbnail prompt writer model context change (VERY LOW)

**What changed:** `thumbnail_prompt_writer` now uses `compose_system_prompt()` instead of `load_prompt()`. The model (GPT-5.5-pro via `config.TD_PLANNING_MODEL`) receives an additional document.

**Risk:** The thumbnail intelligence pipeline describes the execution sequence from a pipeline-architecture perspective. This is descriptive, not prescriptive for the LLM — it explains what happens around the planning step, not what the LLM should output. Risk of interfering with plan output is minimal.

### R-04: `veo_prompt_skill.md` remains on disk (INFORMATIONAL)

**What it is:** A v1.0 Veo skill file that predates the v3.0 `veo_translation_skill.md`. It is registered as SUPERSEDED in the registry but is still present in the repository. If a future developer adds a new import referencing it by name, they will get stale guidance.

**Mitigation:** The skill_registry.py SUPERSEDED entry and rationale make the status explicit. No code path references this file.

---

## How to Verify

### 1. Import verification

```bash
python3 -c "
import os; os.environ.setdefault('OPENROUTER_API_KEY','test')
import hypogeum.agents.visual_package_director as vpd
import hypogeum.agents.shot_diversity_planner as sdp
import hypogeum.agents.thumbnail_prompt_writer as tpw
print('VPD SCENE_PROMPTS_PROMPT length:', len(vpd.SCENE_PROMPTS_PROMPT))
print('SDP SYSTEM_PROMPT length:', len(sdp.SYSTEM_PROMPT))
print('TPW SYSTEM_PROMPT length:', len(tpw.SYSTEM_PROMPT))
print('VPD skill paths:', [p.name for p in vpd._VPD_ALL_SKILL_PATHS])
print('SDP skill paths:', [p.name for p in sdp._SDP_ALL_SKILL_PATHS])
print('TPW skill paths:', [p.name for p in tpw._TPW_ALL_SKILL_PATHS])
"
```

Expected output:
- `VPD SCENE_PROMPTS_PROMPT length: ~140000` (was ~40000 before B0)
- `SDP SYSTEM_PROMPT length: >10000` (was ~base-prompt-only before B0)
- `TPW SYSTEM_PROMPT length: >40000` (was base-prompt-only before B0)

### 2. Registry verification

```bash
python3 -c "
import hypogeum.skill_registry as r
for sid, rec in r.SKILLS.items():
    print(f'{sid}: status={rec.status}, exists={rec.path.exists()}, wired_to={rec.wired_to}')
"
```

Expected: all 7 skills listed; `veo_translation_skill`, `seedance_translation_skill`, `thumbnail_intelligence_pipeline` status=wired; `provider_skill_template` status=governance; `veo_prompt_skill` status=superseded.

### 3. Log event verification

Run any episode through the pipeline and check `.hypogeum-assets/pipeline.log` for:

```bash
grep '"event": "skill_manifest"' .hypogeum-assets/pipeline.log
```

Expected: one `skill_manifest` event per agent run with `loaded_count > 0` and `missing_count = 0`.

### 4. Test suite

```bash
python3 -m pytest -q --tb=short
```

Expected: same pass/fail count as before B0. No new failures.

**Verified result (2026-07-08):**
- Full suite: 9899 passed, 6 failed — identical failure set to pre-B0
- Pre-existing failures (not caused by B0):
  - `test_reuse_engine.py` × 2 — confirmed pre-existing by running on stashed code
  - `test_prompt_registry_phase2b.py[brand_guardian_agent]` — pre-existing modification to `hypogeum/prompts/brand_guardian_agent.txt`
  - `test_prompt_registry_phase2b.py[thumbnail_director]` — pre-existing modification to `hypogeum/prompts/thumbnail_director.txt`
- B0-introduced failures: **0**

---

## Architecture State After B0

```
hypogeum/skills/
├── visual_package_director/
│   └── cinematic_prompt_architecture.md    → VPD Stage A (legacy) + Stage B ✓
├── visual_intelligence/
│   ├── visual_language_engine.md           → VPD Stage A4 + SDP ✓
│   └── thumbnail_intelligence_pipeline.md  → thumbnail_prompt_writer Phase 1 ✓
├── veo_translation_skill.md                → VPD Stage B ✓  [NEW in B0]
├── seedance_translation_skill.md           → VPD Stage B ✓  [NEW in B0]
├── provider_skill_template.md              → GOVERNANCE — not injected (explicit) ✓
└── veo_prompt_skill.md                     → SUPERSEDED — not injected (explicit) ✓

hypogeum/
└── skill_registry.py                       → Central registry (NEW in B0)
```

All 7 internal skills now have explicit, documented status. No skill is silently unused.
