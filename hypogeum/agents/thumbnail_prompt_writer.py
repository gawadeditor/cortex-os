"""
hypogeum/agents/thumbnail_prompt_writer.py — Thumbnail prompt planning (Priority 12 extraction)

Extracted from thumbnail_director.py Phase 1.

Owns:
- Sending the Thumbnail Handoff Brief to Claude via run_agent()
- Injecting thumbnail_concept_brief into the input package when present
- Logging td_planning_started before the call and td_planning_completed after
- Attaching deterministic composition_plan + typography_plan to each
  main_episode candidate in concept_driven_mode (rank → concept mapping)
- Returning the validated thumbnail_generation_plan dict

Concept-driven mode:
    When concept_brief is provided, it is included in the input package under
    "thumbnail_concept_brief". The TD system prompt (SECTION 11C) instructs
    Claude to build image_prompts from the concept fields instead of from
    VPD candidates. Concept A → rank 1, B → rank 2, C → rank 3.

    After Claude returns the plan, composition_plan and typography_plan are
    attached deterministically to each main_episode candidate using the
    thumbnail_composition_planner and thumbnail_typography_planner modules.
    This is authoritative — Claude's own plan fields (if any) are overwritten.

GPT-Image2 skill: SYSTEM_PROMPT composes thumbnail_director.txt (this
agent's own instructions, including a SECTION 12B bridge) with two skills —
the existing internal thumbnail_intelligence_pipeline skill, then the
pinned upstream GPT-Image2 skill
(third_party/skills/gpt-image2/<PINNED_GPT_IMAGE2_SHA>/SKILL.md) — via
hypogeum.prompt_loader.compose_system_prompt(), the same base+skills
composition pattern already used here for the internal skill and for
Humanizer's Script Engine attachment. This happens inside the single
existing run_agent() call below — no second LLM call, no separate stage,
adapter, checkpoint, schema, or provider module. The pinned skill file is
never edited, paraphrased, or copied into this prompt file; it is composed
alongside it, unmodified, matching every other agent's skill injection.
GPT-Image2's own canonical usage is a CLI/OpenAI-SDK wrapper — SECTION 12B
of thumbnail_director.txt exists specifically to establish that this
composed skill contributes prompt-craft guidance only, and disables its
incompatible runtime assumptions (CLI invocation, direct OpenAI calls,
OPENAI_API_KEY access, file writing, interactive questions, its own
standalone output format).

Model: config.TD_PLANNING_MODEL (default: openai/gpt-5.5-pro)
Max tokens: config.TD_PLANNING_MAX_TOKENS (default: 8192)

Public API:
    write_thumbnail_prompts(
        handoff_brief, config, episode_id, output_dir, log,
        concept_brief=None,
    ) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.agents.thumbnail_composition_planner import (
    plan_composition,
    resolve_composition_distinctness,
)
from hypogeum.agents.thumbnail_compositor import build_layer_prompts
from hypogeum.agents.thumbnail_typography_planner import plan_typography
from hypogeum.config import Config
from hypogeum.prompt_composition import SkillSpec, select_skills
from hypogeum.prompt_loader import compose_system_prompt
from hypogeum import skill_registry as _skill_registry
from hypogeum.brand import visual_identity

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_director"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "thumbnail_director.txt"

# Thumbnail Intelligence Pipeline — authoritative stage reference for the
# Thumbnail Director execution sequence (CTR → Composition → Typography → TIG → QA → MUAPI).
# B1: compact runtime version used by default (~2.1K chars vs 4.4K full).
# Full skill retained at hypogeum/skills/visual_intelligence/thumbnail_intelligence_pipeline.md.
_COMPILED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "compiled"
_THUMBNAIL_INTELLIGENCE_SKILL_FILE = _COMPILED_SKILLS_DIR / "thumbnail_intelligence_pipeline.compact.md"

# Pinned upstream GPT-Image2 skill (docs/architecture/UPSTREAM_SKILL_MANIFEST.md §3.6,
# docs/architecture/GPT_IMAGE2_THUMBNAIL_SKILL_ATTACHMENT_AUDIT.md,
# config/upstream_skills_manifest.json) — loaded verbatim, never edited.
PINNED_GPT_IMAGE2_SHA = "74a665173a19e461fa08f84ae76d800f85d2dd1e"
_THIRD_PARTY_ROOT = Path(__file__).resolve().parent.parent.parent / "third_party"
_GPT_IMAGE2_SKILL_PATH = (
    _THIRD_PARTY_ROOT / "skills" / "gpt-image2" / PINNED_GPT_IMAGE2_SHA / "SKILL.md"
)

_TPW_ALL_SKILL_PATHS: list[Path] = [_THUMBNAIL_INTELLIGENCE_SKILL_FILE, _GPT_IMAGE2_SKILL_PATH]

# Per-call, mode-based skill selection (see write_thumbnail_prompts()).
#
# Current matrix: neither attached skill is mode-exclusive in
# thumbnail_director.txt's present content — the GPT-Image2 bridge
# (SECTION 12B) and the Thumbnail Intelligence pipeline map both apply
# regardless of LEGACY_SCENE_MODE vs CONCEPT_DRIVEN_MODE. Both skills are
# therefore marked applicable to both modes, so this slice selects the
# same full stack for both modes today (zero behavior change). Narrowing
# this matrix is a separate, later change — see
# docs/validation/prompt_writer_architecture/IMPLEMENTATION_PLAN.md.
_TPW_SKILL_SPECS: list[SkillSpec] = [
    SkillSpec(_THUMBNAIL_INTELLIGENCE_SKILL_FILE, frozenset({"legacy", "concept_driven"})),
    SkillSpec(_GPT_IMAGE2_SKILL_PATH, frozenset({"legacy", "concept_driven"})),
]


def _load_system_prompt() -> str:
    return compose_system_prompt(
        _PROMPT_FILE, _THUMBNAIL_INTELLIGENCE_SKILL_FILE, _GPT_IMAGE2_SKILL_PATH,
    )


# Retained for backward compatibility: benchmark_thumbnail_prompt_skills.py
# imports this as the "real, unmodified production constant" for its
# condition C. It always represents the full skill stack, independent of
# any specific call's mode. write_thumbnail_prompts() below no longer uses
# it directly — it composes a per-call, mode-selected system prompt instead
# (see select_thumbnail_mode / _TPW_SKILL_SPECS) — but the two are
# byte-identical today because the current selection matrix resolves both
# modes to the same full stack.
SYSTEM_PROMPT: str = _load_system_prompt()


def select_thumbnail_mode(concept_driven: bool) -> str:
    """
    Map the already-computed concept_driven flag to a mode string for
    select_skills(). Kept as a separate function (rather than an inline
    ternary) so it has one clear, independently testable place to grow if
    a genuinely ambiguous signal is ever introduced.
    """
    return "concept_driven" if concept_driven else "legacy"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_thumbnail_prompts(
    handoff_brief: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    concept_brief: Optional[dict] = None,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Send the Thumbnail Handoff Brief (and optionally a concept brief) to Claude
    and return the validated thumbnail_generation_plan.

    When concept_brief is provided, it is included in the input package under
    "thumbnail_concept_brief". The TD system prompt (SECTION 11C) instructs
    Claude to use the three marketing concepts as the primary creative source
    rather than VPD scene candidates. Concept A → rank 1, B → rank 2, C → rank 3.

    The system prompt sent to Claude is composed per call from
    _TPW_SKILL_SPECS via select_skills(), using the mode determined for THIS
    call (select_thumbnail_mode) — never a module-level or import-time
    decision. See _TPW_SKILL_SPECS for the current selection matrix.
    """
    candidates_count = len(handoff_brief.get("candidates", []))
    short_candidates_count = len(handoff_brief.get("short_candidates", []))
    concept_driven = concept_brief is not None and bool(concept_brief.get("concepts"))
    thumbnail_mode = select_thumbnail_mode(concept_driven)

    skill_selection = select_skills(_TPW_SKILL_SPECS, thumbnail_mode)
    system_prompt = compose_system_prompt(_PROMPT_FILE, *skill_selection.paths)
    system_prompt = system_prompt.rstrip() + "\n\n" + visual_identity.build_visual_identity_directive(
        mode="thumbnail_prompt_writer"
    )

    _skill_registry.log_loaded_skills(_AGENT_NAME, list(skill_selection.paths), log)
    visual_identity.log_palette_injection(log, mode="thumbnail_prompt_writer", injected=True)

    log.info(
        "td_planning_started",
        candidates_count=candidates_count,
        short_candidates_count=short_candidates_count,
        concept_driven=concept_driven,
        thumbnail_mode=thumbnail_mode,
        skill_selection_reason=skill_selection.reason,
        skill_count=len(skill_selection.paths),
    )

    input_package: dict = {
        "task": "plan_thumbnail_generation",
        "thumbnail_handoff_brief": handoff_brief,
    }
    if concept_driven:
        input_package["thumbnail_concept_brief"] = concept_brief
    try:
        from hypogeum.channel_creative_brief import channel_brief_for
        input_package["channel_brief"] = channel_brief_for(channel_id)
    except Exception:  # noqa: BLE001
        pass

    generation_plan = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=config.TD_PLANNING_MODEL,
        system_prompt=system_prompt,
        input_package=input_package,
        output_schema="thumbnail_generation_plan",
        episode_id=episode_id,
        max_tokens=config.TD_PLANNING_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
        channel_id=channel_id,
    )

    if concept_driven:
        _attach_concept_plans(generation_plan, concept_brief)

    log.info(
        "td_planning_completed",
        main_candidates_planned=len(
            generation_plan.get("main_episode", {}).get("ranked_candidates", [])
        ),
        shorts_count=len(generation_plan.get("shorts", [])),
        concept_driven=concept_driven,
    )

    return generation_plan


# ---------------------------------------------------------------------------
# Internal: concept plan attachment
# ---------------------------------------------------------------------------

def _attach_concept_plans(generation_plan: dict, concept_brief: dict) -> None:
    """
    Attach deterministic composition_plan, typography_plan, layer_prompts,
    typography_integration, and composite_mode to every main_episode
    ranked_candidate.

    Routing (Thumbnail DNA v1, 2026-07-13 — approved primary path correction):
      typography_integration == "embedded" (every current trigger in
      thumbnail_composition_planner._TRIGGER_MAP) -> composite_mode=False.
      thumbnail_generation_agent.generate_thumbnails() routes this to
      generate_unified_integrated_thumbnail() — one unified GPT Image 2 call
      with typography baked in, not the layered compositor.

      typography_integration == "composited" (only thumbnail_composition_
      planner._FALLBACK, i.e. an unrecognized emotional_trigger) ->
      composite_mode=True, the proven layered path, as a safety net.

    layer_prompts is still computed and attached in both cases — this
    preserves rollback capability to the layered path without removing it,
    even though the unified path does not read layer_prompts.

    Mapping: concepts[0] → rank 1, concepts[1] → rank 2, concepts[2] → rank 3.
    Mutates generation_plan in place.

    After every candidate's composition_plan is attached from the frozen,
    per-trigger _TRIGGER_MAP defaults, resolve_composition_distinctness()
    runs once over the full assembled set to deterministically de-collide
    focal_zone across the ranked A/B/C candidates (see that function's
    docstring in thumbnail_composition_planner.py) — required because most
    triggers deliberately share the same CTR-optimized focal_zone, which
    would otherwise fail thumbnail_qa's composition_distinctness check.
    Rank 1 is never touched by that pass.
    """
    concepts = concept_brief.get("concepts", [])
    candidates = generation_plan.get("main_episode", {}).get("ranked_candidates", [])

    concept_by_rank: dict[int, dict] = {i + 1: c for i, c in enumerate(concepts)}

    for candidate in candidates:
        rank = candidate.get("rank", 0)
        concept = concept_by_rank.get(rank)
        if concept is None:
            continue
        trigger   = concept.get("emotional_trigger", "")
        hook_text = concept.get("suggested_hook_text", "")
        comp_plan = plan_composition(trigger)
        typo_plan = plan_typography(hook_text, trigger, comp_plan)
        typography_integration = comp_plan.get("typography_integration", "composited")

        candidate["composition_plan"]       = comp_plan
        candidate["typography_plan"]        = typo_plan
        candidate["typography_integration"] = typography_integration
        candidate["layer_prompts"]          = build_layer_prompts(concept, comp_plan)
        candidate["composite_mode"]         = (typography_integration != "embedded")

    resolve_composition_distinctness(candidates)


def _embed_typography_in_prompt(base_prompt: str, typography_plan: dict) -> str:
    """
    Append an integrated typography instruction block to a base image_prompt.

    The instruction tells the image model to render the hook text INSIDE the
    generated image — not as a subtitle or overlay, but as physically integrated
    text that receives the scene's lighting, shadows, and depth.

    The PIL compositor will skip typography rendering when typography_integration
    is "embedded", trusting the image model to have rendered it correctly.

    Typography-only building block — owns exactly: exact headline text, line
    breaks, font character, scale relationship, placement, spelling. Does NOT
    restate frame-share/width percentages, mobile readability, or hierarchy —
    those are owned by hypogeum.agents.thumbnail_composition_planner.
    describe_dna_constraints_compact(). Does NOT append the brand
    visual-identity directive or a hardcoded color (a hardcoded hex here would
    be a second, uncoordinated palette source — colors come from
    hypogeum.brand.visual_identity only). Callers (e.g.
    hypogeum.agents.thumbnail_compositor.generate_unified_integrated_thumbnail())
    own composing the full unified prompt from all required components, each
    exactly once, in a single well-defined order.

    2026-07-13 (V5.1 prompt-length/quote-consistency correction): previously
    used Python's !r format spec for quoting, which silently switches between
    single and double quotes depending on whether the string itself contains
    an apostrophe (repr("EXIST") -> 'EXIST' but repr("IT SHOULDN'T") ->
    "IT SHOULDN'T") — an unintentional, inconsistent punctuation pattern in
    the submitted prompt. Now uses explicit double-quoting throughout.
    """
    headline      = typography_plan.get("headline", "").upper()
    emphasis      = typography_plan.get("emphasis", "").upper()
    zone          = typography_plan.get("zone", "lower_left")
    emph_scale    = typography_plan.get("emphasis_scale", 1.7)

    zone_desc = {
        "lower_left":  "bottom-left quadrant of the frame",
        "lower_right": "bottom-right quadrant of the frame",
        "lower_center":"bottom-center of the frame",
        "center":      "center of the frame",
    }.get(zone, "bottom-left quadrant of the frame")

    # Describe the hierarchy: headline small above, emphasis huge below
    if headline and emphasis:
        text_block = f'"{headline}" (smaller, upper line), then "{emphasis}" (dramatically larger, directly below)'
        scale_note = f' "{emphasis}" is approximately {emph_scale:.0f}x taller than "{headline}".'
    elif emphasis:
        text_block = f'"{emphasis}" (dramatically larger)'
        scale_note = ""
    else:
        return base_prompt

    integration_block = (
        f" — INTEGRATED TYPOGRAPHY ({zone_desc}): exact text reads {text_block}."
        f"{scale_note} Carved weathered stone letterforms, restrained metallic "
        f"edge highlight only — not a solid glowing fill. Physically integrated "
        f"into the scene's own lighting, not a subtitle, overlay, box, or "
        f"lower-third. Correctly spelled, fully legible."
    )

    return base_prompt + integration_block
