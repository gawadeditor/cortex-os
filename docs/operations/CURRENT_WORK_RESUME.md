# Current Work Resume — Hypogeum Brand/Thumbnail Quality System

**Last updated:** 2026-07-13
**Machine-readable companion:** `docs/operations/current_work_state.json`

---

## Current priority

Freeze, document, and protect the Brand Bible / Thumbnail DNA /
unified-integrated-thumbnail quality system as **permanent, tested,
production-active project DNA** — before attempting any full episode.
Full episode generation is explicitly **not** the current priority and
must not be started without explicit user authorization.

---

## What was completed this phase

1. **Brand Bible v1.1** (`docs/brand_guardian/brand_bible_hypogeum.json`) —
   canonical 7-swatch palette, usage ratios, forbidden color behavior,
   visual finish rules. `_global_status: "active"`, approved by Mohamed
   Abdelgawad, 2026-07-13.
2. **`hypogeum/brand/visual_identity.py`** — single loader for the Brand
   Bible. Three context-aware enforcement tiers:
   - **FULL** (thumbnail/endscreen/channel_artwork/generic) — exact palette
     ratios, forbidden casts, finish rules, verbatim.
   - **ADAPTIVE** (video_scene) — qualitative Hypogeum DNA, no frame-share
     percentages, preserves historically/environmentally accurate colors.
   - **LIGHT** (reference_image) — entity continuity and material accuracy
     first, no palette percentages.
   Each tier now has both a verbose builder (docs/validation) and, for FULL,
   a compact builder (provider submission — see #5).
3. **`hypogeum/brand/palette_validator.py`** — perceptual, tolerance-based
   post-generation palette compliance checker for thumbnails.
4. **Thumbnail DNA v2** (`hypogeum/agents/thumbnail_composition_planner.py`)
   — topic-agnostic composition rules: headline zone width, emphasis-line
   cap, hero-object max frame share, concealment level, detail density,
   patina prominence cap, explicit primary/secondary/tertiary
   `focal_priority`, human-figure gesture/mobile-readability requirements.
   Enforced by `hypogeum/agents/thumbnail_plan_validator.py::
   validate_thumbnail_dna()`, wired into the real pre-generation gate in
   `hypogeum/agents/thumbnail_director.py`.
5. **Unified integrated-thumbnail production route** —
   `hypogeum/agents/thumbnail_compositor.py::
   generate_unified_integrated_thumbnail()` is now the approved primary
   path for every recognized `emotional_trigger`: one MUAPI call, four
   prompt components (topic concept → integrated typography → composition
   DNA → brand directive), each exactly once. The layered
   `generate_and_composite()` path is preserved, not removed, and remains
   the automatic fallback for an unrecognized trigger.
6. **Provider-safe compact prompt (V5.1)** — after two real V5 generation
   attempts failed on MUAPI (`"Internal Error"`, then `"The current content
   could not be processed"`), audited the exact 6,730-character composed
   prompt, found the same concepts restated 2–7 times, and built compact
   directive renderers (`describe_dna_constraints_compact()`,
   `build_visual_identity_directive_compact()`) that preserve every
   structured constraint while cutting total prompt length by 63% (6,730 →
   2,467 characters, well under the 4,500 ceiling). Full/verbose builders
   remain unchanged for validation and documentation.
   See `docs/validation/thumbnail_image_generation_validation/
   ANTIKYTHERA_COMPOSITION_BLUEPRINT_V5_1.md` for the full audit.
7. **Expensive-run safety gate** (`hypogeum/production_gate.py`) — full
   paid episode production is blocked by default. Reuses existing
   config/budget/preflight systems (no new dollar-amount policy invented).
   Wired into `hypogeum/pipeline.py::run_pipeline()` — the single choke
   point every production path (Dashboard → worker → queue, or the CLI)
   funnels through, so nothing can bypass it.

---

## What is active in real production paths right now

- Every concept-driven thumbnail candidate is routed to the unified
  integrated path by default (`composite_mode=False`,
  `typography_integration="embedded"`).
- `validate_thumbnail_dna()` runs as a blocking pre-generation gate in
  `run_thumbnail_director()` — a violation raises
  `ThumbnailPreGenerationError` before any paid call.
- `require_full_production_authorized()` runs at the very top of
  `run_pipeline()`, before the Executive Producer or any other agent —
  a default `Config()` raises `ProductionGateBlocked` immediately.
- Brand visual-identity directives (full/adaptive/light) are injected
  automatically into thumbnail, video-scene, and reference-image
  generation prompts — no manual copy-paste from a Markdown file required.

## What remains incomplete

- **V5.1 has not been tested against the real provider yet.** Only V5
  (the longer, pre-compaction prompt) failed twice on MUAPI. A real
  generation is the only way to confirm the length/duplication fix
  actually resolves the rejection.
- Adaptive (video_scene) and light (reference_image) brand directives were
  not compacted — they were already shorter and are not the ones that
  caused a provider rejection. Revisit only if a similar rejection occurs
  on those paths.
- No cost-estimate UI/preview was built for the safety gate beyond reusing
  `hypogeum.setup_preflight.get_setup_preflight_status()`'s existing
  `estimated_cost_low`/`estimated_cost_high` fields — a dedicated
  "estimated call count and cost" breakdown per-agent was explicitly out
  of scope for this pass (Dashboard Operator Console work is paused).
- Dashboard Operator Console / queue / autostart feature work is
  **explicitly paused** per this session's priority correction — do not
  resume without the user re-raising it.

---

## Latest successful tests

**1,101 passed, 0 failed** — targeted suite across every system touched
this phase: `test_thumbnail_composition_dna.py`, `test_brand_visual_
identity.py`, `test_thumbnail_unified_integrated_path.py`,
`test_thumbnail_compositor_regression.py`, `test_thumbnail_prompt_
writer.py`, `test_thumbnail_director.py`, `test_thumbnail_generation_
agent.py`, `test_thumbnail_fixes.py`, `test_thumbnail_composition_
typography.py`, `test_thumbnail_recovery.py`, `test_thumbnail_strategy*.py`,
`test_visual_package_director.py`, `test_brand_guardian_agent.py`,
`test_provider_registry.py`, `test_entity_continuity_manager.py`,
`test_muapi.py`, `test_production_gate.py` (new), `test_pipeline.py`,
`test_config.py`.

The most recent **full-suite** run (several turns before this state file,
prior to the V5.1/safety-gate work) was 10,223 passed / 2 failed / 6–8
deselected — see "Known unrelated failures" below. A full-suite rerun was
not repeated after the V5.1/safety-gate work due to session time budget;
the 1,101-test targeted suite covers every file touched since.

## Latest provider failure details (superseded 2026-07-19 — see below)

Two real MUAPI attempts on the Antikythera V5 unified thumbnail (episode
`ep_003`), both through the real production route
(`generate_unified_integrated_thumbnail()`), both with `apply_wordmark`
disabled via a runtime patch for review purposes:

1. `request_id=2dbba9cf-6dd7-4364-887b-e2dd848e9cba` — generic
   `"Internal Error, Please try again later."` (HTTP 400 × 3, early-abort).
2. `request_id=3d19cc49-d7ec-4737-8a35-e97185180b43` (new submission,
   reached `"processing"` status) — `"The current content could not be
   processed. Please revise your input and try again."` (HTTP 400 × 3,
   early-abort).

Root cause: the composed prompt was 6,730 characters against a documented
30–60 word design target. Fixed as V5.1 (2,467 characters).

**2026-07-19 update — V5.1 now confirmed against the real provider.** Two
full supervised pipeline validation runs (episodes `ep_74237788` and
`ep_1f827369`, real topics, real production route). `MUAPI_DRY_RUN` was
active for video-clip generation only (that run's own purpose was
validating the music architecture); thumbnail generation was a real,
paid MUAPI call both times, and hit **zero**
`"Internal Error"` / `"content could not be processed"` rejections. The length/duplication fix holds. Run 2 surfaced one new,
different, now-fixed issue instead: `thumbnail_composition_distinctness`
(all 3 A/B/C candidates shared `focal_zone='center_right'`). Full root
cause and fix: `composition_distinctness_resolution_2026_07_19` in
`current_work_state.json`. Summary: `_TRIGGER_MAP`'s CTR-first
consolidation (2026-06-21) and the distinctness QA check (2026-07-01) were
never reconciled; fixed at the candidate-assembly layer
(`thumbnail_prompt_writer.py::_attach_concept_plans()` now calls
`thumbnail_composition_planner.py::resolve_composition_distinctness()`),
`_TRIGGER_MAP` and Rank 1 both left untouched. 20 new regression tests;
640-test targeted thumbnail suite passes.

---

## Exact next task

**None for this workstream.** With V5.1 confirmed against the real
provider and the composition-distinctness gap fixed, the Brand Bible /
Thumbnail DNA / unified-integrated-thumbnail system is considered frozen
and production-active (see `current_priority` in `current_work_state.json`).
The next Hypogeum roadmap priority is chosen separately — this document's
own scope is complete. Two unrelated blockers to a real published episode
remain (YouTube OAuth token expired; see the 2026-07-19 validation report
in session history) but belong to different workstreams, not this one.

### Exact next Claude prompt (if the user wants to resume with minimal re-explanation)

> "Read docs/operations/CURRENT_WORK_RESUME.md and current_work_state.json.
> Confirm the Thumbnail DNA / Brand Bible / unified-integrated-thumbnail
> system is still in the state described. Then [generate one Antikythera
> V5.1 review thumbnail through the real production route / or: whatever
> the user specifies]. Do not start a full episode. Do not enable
> FULL_PRODUCTION_ENABLED without being told to."

---

## Files changed this phase

**New:**
- `hypogeum/brand/visual_identity.py`
- `hypogeum/brand/palette_validator.py`
- `hypogeum/production_gate.py`
- `tests/test_brand_visual_identity.py`
- `tests/test_thumbnail_composition_dna.py`
- `tests/test_thumbnail_unified_integrated_path.py`
- `tests/test_production_gate.py`
- `docs/validation/thumbnail_image_generation_validation/` (V3, V4, V5,
  V5.1 blueprints + earlier batch-validation artifacts)
- `docs/operations/CURRENT_WORK_RESUME.md` (this file)
- `docs/operations/current_work_state.json`

**Modified:**
- `docs/brand_guardian/brand_bible_hypogeum.json`
- `hypogeum/agents/entity_continuity_manager.py`
- `hypogeum/agents/thumbnail_composition_planner.py`
- `hypogeum/agents/thumbnail_compositor.py`
- `hypogeum/agents/thumbnail_director.py`
- `hypogeum/agents/thumbnail_generation_agent.py`
- `hypogeum/agents/thumbnail_plan_validator.py`
- `hypogeum/agents/thumbnail_prompt_writer.py`
- `hypogeum/config.py`
- `hypogeum/muapi.py`
- `hypogeum/pipeline.py`
- `hypogeum/prompts/brand_guardian_agent.txt`
- `tests/conftest.py`
- `tests/test_entity_continuity_manager.py`, `tests/test_muapi.py`,
  `tests/test_provider_registry.py`, `tests/test_thumbnail_composition_
  typography.py`, `tests/test_thumbnail_compositor_regression.py`,
  `tests/test_thumbnail_prompt_writer.py`

## Known unrelated failures (do not attempt to fix as part of this work)

1. `tests/test_prompt_files_phase2a.py` / `test_prompt_registry_phase2b.py`
   — `thumbnail_director.txt` and `vpd_scene_prompts` carry
   `PROMPT_VERSION='v3'`, these tests assert v1/v2 only. Pre-existing, from
   an earlier "conditional prompt skill selection" task this session.
   Confirmed via `git diff` to be untouched by any Brand Bible/DNA/gate
   work.
2. `tests/test_reuse_engine.py` — `TestScoreAsset`/`TestScoringEdgeCases`
   assertions are wall-clock-time-dependent (recency-decay scoring) and
   have failed intermittently on different sub-tests across different
   full-suite runs. Confirmed via `git diff`: `reuse_engine.py` and its
   test file were never touched this session.

---

## Prohibition — read before doing anything else

**Do not generate a full episode.** `FULL_PRODUCTION_ENABLED=false` and
`PRODUCTION_MODE=validation` by design — this is intentional and must stay
this way until the user explicitly decides to move past the validation
phase.

## How to safely authorize one review generation

Single-image/single-scene review generations do **not** go through
`hypogeum.pipeline.run_pipeline()` and are therefore unaffected by the
production gate — they are standalone scripts calling a single function
(e.g. `generate_unified_integrated_thumbnail()`) directly with a real
`Config()` and a `MUAPI_DRY_RUN=false` process-level environment override.
The safety mechanism for these is **conversational, not code-level**: only
generate when the user explicitly asks for exactly one generation in the
current turn. Never batch, never retry beyond what's explicitly authorized,
never reuse a prior authorization for a new generation.

## How to enable full production later

1. Set `FINANCE_BUDGET_CEILING_USD` (or `MUAPI_EPISODE_BUDGET_CREDITS`) to
   a real, approved value in `.env` — this repo does not hardcode a dollar
   figure; the user/operator sets their own approved ceiling.
2. Confirm `python3 hypogeum/setup_preflight.py`-equivalent (or the
   Dashboard's `/api/setup/preflight` endpoint) reports `"ready": true`
   with zero blockers.
3. Set `PRODUCTION_MODE=production` and `FULL_PRODUCTION_ENABLED=true` in
   `.env`.
4. Confirm provider/model configuration
   (`THUMBNAIL_IMAGE_PROVIDER=muapi`, `THUMBNAIL_IMAGE_MODEL=gpt-image-2-
   text-to-image`, `MUAPI_DRY_RUN=false`) is intentional, not left over
   from a validation session.
5. Only then does `hypogeum.pipeline.run_pipeline()` (and therefore the
   Dashboard's worker-spawn buttons and the CLI) proceed past the
   `require_full_production_authorized()` gate.
