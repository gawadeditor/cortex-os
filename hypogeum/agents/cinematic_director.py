"""
hypogeum/agents/cinematic_director.py — Cinematic Director

Production review of the first completed episode found near-identical shots
generated to cover the same narration beat (e.g. three near-identical rear
views of the same crawling figure) — a real directorial planning gap, not a
prompt-wording problem. Root cause: Visual Package Director planned exactly
one shot per beat (see hypogeum.agents.visual_package_director's Stage A2:
"plan ONE scene per narration beat... no more, no fewer"), so any additional
footage a long beat needed was only ever produced reactively, after the fact,
by clip_repair_agent.fill_coverage_deficits() re-submitting that ONE scene's
identical prompt again for extra1/extra2/... with no purpose, order, or
diversity concept at all.

This module introduces the missing planning layer, inserted between Stage A4
(presentation planning) and Shot Diversity Planner in visual_package_director.
py's orchestration:

    Narrative Beat → Cinematic Coverage Plan → Ordered Shot Intents →
    (Shot Diversity Planner, per shot) → (Stage B, one prompt per shot)

For every beat, this module decides:
  - how many shots the beat needs (duration-aware, never a fixed count —
    see compute_shot_budget(), the sole deterministic, zero-LLM-call
    function in this module)
  - what each shot contributes (shot_purpose, narrative_function)
  - the ordered visual progression across those shots (progression_pattern)
  - clean-cut compatibility (transition_in/transition_out are always
    "clean_cut" per channel directing policy — see hypogeum.channel_config)

Providers are never made aware of this layer. A planned shot becomes, at
generation time, an ordinary scene-shaped submit_scene() request identical
in every respect to how VPD's Phase 2 or clip_repair_agent already submit
one today — this module only decides WHAT to ask for and WHY, never HOW to
ask a specific provider for it.

Public API:
    compute_shot_budget(narration_duration_seconds, config) -> int
    run_cinematic_director(visual_package, beat_texts_by_id, episode_id,
                            config, output_dir=None, *,
                            measured_durations_by_id=None) -> dict
    shot_diversity_key(shot) -> tuple                            (2026-08-07)
    validate_coverage_plan_diversity(coverage_plan, episode_id) -> None  (2026-08-07)
    CoveragePlanDiversityError                                   (2026-08-07)

Checkpoint: cinematic_coverage_plan_{episode_id}.json

Logging events:
    cinematic_director_started      — info
    cinematic_director_resumed      — info  — checkpoint found, skipping call
    cinematic_director_completed    — info  — plan written to checkpoint
    cinematic_director_failed       — error — LLM call failed, pass-through used

Ownership addendum (2026-08-07, refactor after the coverage-fill shot-
diversity fix): this module is the sole owner of "what makes two planned
shots the same shot" (shot_diversity_key()) and of enforcing its own
prompt file's WITHIN-BEAT DIVERSITY rule at planning time
(validate_coverage_plan_diversity(), wired into visual_package_director.py
immediately after run_cinematic_director() returns — rejects the Visual
Package before Shot Diversity Planner, Stage B, or any MUAPI/provider call
runs for that episode). hypogeum.agents.clip_repair_agent imports
shot_diversity_key() from here for its own (secondary, defense-in-depth)
fail-closed check rather than reimplementing the rule — this module is the
single source of truth for it. Rendering a planned shot's structured
fields into actual prompt text is NOT this module's job — see
hypogeum.video_prompt_writer.render_coverage_plan_shot_prompts(), called
by visual_package_director.py right after Stage B writes each scene's own
base prompt. This module only ever decides shot IDENTITY (what the shot
is, camera/composition fields, whether it duplicates a sibling) — never
prompt TEXT.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.prompt_loader import compose_system_prompt

_AGENT_NAME = "cinematic_director"
_CLAUDE_SONNET = "anthropic/claude-sonnet-4.6"
_MAX_TOKENS = 4_096

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "cinematic_director.txt"

_DEFAULT_ASSETS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / ".hypogeum-assets"
)

# Directorial progression vocabulary (Phase 5) — the LLM selects the pattern
# that fits each beat's narration; this module never mechanically forces the
# same pattern onto every beat. A single-shot beat has no real progression
# and always uses "single_shot".
PROGRESSION_PATTERNS = (
    "single_shot",
    "establish_develop_reveal",
    "environment_character_detail",
    "wide_medium_close",
    "mystery_evidence_discovery",
    "action_reaction_consequence",
    "exterior_interior_object",
)


def _load_prompt() -> str:
    return compose_system_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_prompt()


def estimate_beat_duration_seconds(text: str, config: Config) -> float:
    """
    Word-count-based narration-duration estimate for one beat's raw text.

    Cinematic Director runs before Audio Agent (Stage order: ... -> VPD ->
    ... -> Audio -> Assembly), so the real, ElevenLabs-measured narration
    duration does not exist yet at planning time -- only script_package's
    raw beat text does. This estimate drives how many shots get PLANNED;
    it is never treated as authoritative downstream. coverage_check.py and
    Assembly's coverage gate still measure and enforce the REAL narration
    duration once Audio Agent has run, exactly as they do today.
    """
    wpm = max(1.0, getattr(config, "CINEMATIC_ESTIMATED_WORDS_PER_MINUTE", 145.0))
    word_count = len((text or "").split())
    return round((word_count / wpm) * 60.0, 3)


def compute_shot_budget(narration_duration_seconds: float, config: Config) -> int:
    """
    Deterministic, zero-LLM-call shot-count budget for one beat (Phase 4).

    Three independent terms, kept separate and never conflated:

      coverage_minimum  = ceil(duration / coverage_capacity_per_shot)
        The number of shots physically required for this beat's real
        footage to reach its narration duration, given what one generated
        clip actually supplies (the currently selected provider/model's
        requested clip duration — muapi.get_requested_clip_duration_seconds(),
        never hardcoded here) stretched by at most the approved editorial
        speed factor (coverage_check.MAX_EDITORIAL_SPEED_FACTOR — the same
        ceiling Assembly's own coverage gate enforces, so planning can
        never under-provision relative to what Assembly will actually
        require).

      editorial_minimum = ceil(duration / editorial_pacing_target)
        A directorial pacing preference (config.CINEMATIC_SECONDS_PER_SHOT,
        default 10.0) — how much screen time one shot idea is expected to
        comfortably cover before a cut is warranted. A style choice,
        independent of physical clip supply; kept as its own term so a
        future provider/policy combination where it legitimately dominates
        (e.g. a provider with much longer clips than today's) is handled
        correctly without further changes here.

      policy_minimum = config.CINEMATIC_MIN_SHOTS_PER_BEAT (default 1)
        An explicit, configurable floor, replacing what was previously a
        hardcoded "at least 1" literal.

    planned_shots = max(coverage_minimum, editorial_minimum, policy_minimum),
    clamped at config.CINEMATIC_MAX_SHOTS_PER_BEAT as a finite safety cap so
    a single very long beat cannot request unbounded coverage.
    """
    from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR  # noqa: PLC0415
    from hypogeum.muapi import get_requested_clip_duration_seconds  # noqa: PLC0415

    duration = max(0.0, narration_duration_seconds)
    editorial_pacing_target = max(0.1, getattr(config, "CINEMATIC_SECONDS_PER_SHOT", 10.0))
    max_shots = max(1, getattr(config, "CINEMATIC_MAX_SHOTS_PER_BEAT", 6))
    policy_minimum = max(1, getattr(config, "CINEMATIC_MIN_SHOTS_PER_BEAT", 1))

    provider_clip_duration = get_requested_clip_duration_seconds(config)
    coverage_capacity_per_shot = max(0.1, provider_clip_duration * MAX_EDITORIAL_SPEED_FACTOR)

    coverage_minimum = math.ceil(duration / coverage_capacity_per_shot)
    editorial_minimum = math.ceil(duration / editorial_pacing_target)
    planned_shots = max(coverage_minimum, editorial_minimum, policy_minimum)
    return min(planned_shots, max_shots)


def run_cinematic_director(
    visual_package: dict,
    beat_texts_by_id: dict,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
    *,
    measured_durations_by_id: Optional[dict] = None,
) -> dict:
    """
    Produce a Cinematic Coverage Plan for every beat in the episode.

    Args:
        visual_package:    Visual Package dict (after Stage A4 has injected
                            presentation_plan into each scene).
        beat_texts_by_id:  {beat_id: raw narration text}. Used directly only
                            as a fallback estimate (see
                            estimate_beat_duration_seconds()) for any
                            beat_id missing from measured_durations_by_id —
                            the normal case is that Audio Agent already ran
                            and every beat has a real measured duration.
        episode_id:         Episode identifier for logging and file naming.
        config:             Loaded Config instance.
        output_dir:         Optional output directory override.
        measured_durations_by_id: {beat_id: real narration duration in
                            seconds}, from the validated narration_package
                            (Audio Agent's output) — the authoritative
                            temporal planning input, keyword-only and
                            optional (defaults to {}). Takes precedence over
                            the text-based estimate whenever present; a
                            caller that omits it (e.g. an existing test not
                            concerned with duration accuracy) falls back to
                            the per-beat word-count estimate exactly as
                            before.

    Returns:
        cinematic_coverage_plan dict (schema-validated, written to disk).
        On LLM failure, returns a minimal valid single-shot-per-beat plan
        (transition_in/out=clean_cut, no invented purpose/progression
        beyond a safe generic default) so the pipeline can continue.
    """
    _resolve_dir: Path = (
        Path(output_dir) if output_dir is not None
        else _DEFAULT_ASSETS_ROOT / "episodes" / episode_id
    )
    _resolve_dir.mkdir(parents=True, exist_ok=True)

    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)
    ckpt_path = _resolve_dir / f"cinematic_coverage_plan_{episode_id}.json"

    if ckpt_path.exists():
        log.info("cinematic_director_resumed", path=str(ckpt_path), episode_id=episode_id)
        return json.loads(ckpt_path.read_text(encoding="utf-8"))

    scenes = visual_package.get("scenes", [])
    measured_durations_by_id = measured_durations_by_id or {}

    beats_for_director = []
    for scene in scenes:
        beat_id = scene.get("narration_reference", "")
        measured_duration = measured_durations_by_id.get(beat_id)
        if measured_duration is not None:
            narration_duration = float(measured_duration)
        else:
            beat_text = beat_texts_by_id.get(beat_id, "")
            narration_duration = estimate_beat_duration_seconds(beat_text, config)
            log.warn(
                "cinematic_director_measured_duration_missing",
                episode_id=episode_id,
                beat_id=beat_id,
                fallback="word_count_estimate",
            )
        shot_budget = compute_shot_budget(narration_duration, config)
        beats_for_director.append({
            "beat_id": beat_id,
            "scene_id": scene.get("scene_id"),
            "source_type": scene.get("source_type"),
            "narration_duration_seconds": round(narration_duration, 3),
            "shot_budget": shot_budget,
            "visual_spec": scene.get("visual_spec", {}),
            "presentation_plan": scene.get("presentation_plan", {}),
            "narrative_signals": scene.get("narrative_signals", []),
        })

    director_input = {
        "task": "plan_cinematic_coverage",
        "episode_metadata": {
            "episode_title": visual_package.get("episode_metadata", {}).get("episode_title", ""),
            "total_beats": len(beats_for_director),
            "episode_id": episode_id,
        },
        "progression_patterns": list(PROGRESSION_PATTERNS),
        "beats": beats_for_director,
    }

    log.info(
        "cinematic_director_started",
        episode_id=episode_id,
        beats=len(beats_for_director),
        total_shot_budget=sum(b["shot_budget"] for b in beats_for_director),
        max_tokens=_MAX_TOKENS,
    )

    try:
        result = run_agent(
            agent_name=_AGENT_NAME,
            model_slug=_CLAUDE_SONNET,
            system_prompt=SYSTEM_PROMPT,
            input_package=director_input,
            output_schema="cinematic_coverage_plan",
            episode_id=episode_id,
            max_tokens=_MAX_TOKENS,
            config=config,
            output_dir=None,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "cinematic_director_failed",
            episode_id=episode_id,
            error=str(exc),
            note="Returning minimal single-shot-per-beat pass-through plan",
        )
        result = _minimal_pass_through(episode_id, beats_for_director)

    ckpt_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        "cinematic_director_completed",
        episode_id=episode_id,
        beat_plans=len(result.get("beat_plans", [])),
        total_shots=sum(len(bp.get("shots", [])) for bp in result.get("beat_plans", [])),
        path=str(ckpt_path),
    )
    return result


def _minimal_pass_through(episode_id: str, beats_for_director: list) -> dict:
    """
    Minimal valid cinematic_coverage_plan when the LLM call fails: exactly
    one shot per beat (the same coverage the pre-existing pipeline already
    provided), so a director-layer outage degrades to today's known-safe
    behavior rather than blocking the episode.
    """
    beat_plans = []
    for b in beats_for_director:
        beat_id = b["beat_id"]
        beat_plans.append({
            "beat_id": beat_id,
            "narration_duration_seconds": b["narration_duration_seconds"],
            "progression_pattern": "single_shot",
            "shots": [{
                "shot_id": f"{beat_id}__shot1",
                "parent_beat_id": beat_id,
                "shot_order": 1,
                "shot_purpose": "establish_location",
                "narrative_function": "primary coverage for this beat",
                "estimated_usable_duration": 8.0,
                "subject": b["visual_spec"].get("subject", ""),
                "action": "",
                "framing": b["visual_spec"].get("framing", ""),
                "shot_scale": "medium",
                "camera_angle": "eye_level",
                "camera_height": "eye_level",
                "camera_direction": "frontal",
                "camera_movement": "slow_push_in",
                "lens_character": "standard",
                "composition": "rule_of_thirds_left",
                "foreground_usage": "none",
                "background_depth": "shallow",
                "lighting_direction": b["visual_spec"].get("lighting", ""),
                "screen_direction": "neutral",
                "eye_line": "neutral",
                "motion_energy": "low",
                "transition_in": "clean_cut",
                "transition_out": "clean_cut",
                "diversity_justification": None,
            }],
        })
    return {"episode_id": episode_id, "beat_plans": beat_plans, "notes": ["pass_through_fallback"]}


# ---------------------------------------------------------------------------
# Shot diversity — identity, not text (2026-08-07)
#
# This module decides what a shot IS (camera/composition/purpose fields);
# it never decides what a shot's prompt TEXT says. The fields below are the
# same set this module's own prompt file (hypogeum/prompts/cinematic_
# director.txt, "WITHIN-BEAT DIVERSITY") already documents as making two
# shots the same shot to a viewer. shot_diversity_key() is the single
# source of truth for that rule — both validate_coverage_plan_diversity()
# below (planning-stage rejection) and hypogeum.agents.clip_repair_agent's
# own fail-closed slot-assignment check import it, rather than each
# encoding the rule independently.
# ---------------------------------------------------------------------------

SHOT_DIVERSITY_FIELDS: tuple = (
    "shot_scale", "camera_angle", "camera_height", "camera_direction",
    "camera_movement", "composition", "background_depth", "screen_direction",
)


def shot_diversity_key(shot: dict) -> tuple:
    """A hashable signature of `shot`'s camera/composition design. Two
    shots in the same beat with the same key are the same shot for
    coverage purposes, unless the later one sets diversity_justification."""
    return tuple(shot.get(field) for field in SHOT_DIVERSITY_FIELDS)


class CoveragePlanDiversityError(Exception):
    """
    Raised by validate_coverage_plan_diversity() when a Cinematic Coverage
    Plan contains two shots in the same beat with an identical
    shot_diversity_key() and no diversity_justification on the later one —
    exactly the planning failure this module's own prompt file's "THE
    CENTRAL RULE" section describes (shots technically different but
    communicating the same thing).

    A planning-stage rejection: raised before Shot Diversity Planner,
    Stage B (prompt writing), or any MUAPI/provider call runs for the
    episode — no generation credit is ever spent on a plan this module
    itself would already consider a directing failure.
    """

    def __init__(self, episode_id: str, beat_id: str, shot_id_a: str, shot_id_b: str):
        self.episode_id = episode_id
        self.beat_id = beat_id
        self.shot_id_a = shot_id_a
        self.shot_id_b = shot_id_b
        super().__init__(
            f"Cinematic Coverage Plan rejected for episode {episode_id!r}: "
            f"beat {beat_id!r} has compositionally equivalent shots "
            f"{shot_id_a!r} and {shot_id_b!r} (same shot_scale/camera_angle/"
            "camera_height/camera_direction/camera_movement/composition/"
            "background_depth/screen_direction) with no diversity_justification."
        )


def validate_coverage_plan_diversity(coverage_plan: dict, episode_id: str) -> None:
    """
    Raise CoveragePlanDiversityError if any beat_plan in `coverage_plan`
    assigns the same shot_diversity_key() to two shots without an explicit
    diversity_justification on the later one (shot_order-wise).

    Pure validation — never mutates `coverage_plan`. A beat with
    shot_budget=1 (the overwhelmingly common case, and the whole of
    _minimal_pass_through()'s LLM-failure fallback) trivially passes: a
    single shot cannot duplicate a sibling that doesn't exist.
    """
    for bp in coverage_plan.get("beat_plans", []):
        beat_id = bp.get("beat_id")
        seen: dict = {}
        for shot in sorted(bp.get("shots", []), key=lambda s: s.get("shot_order", 0)):
            key = shot_diversity_key(shot)
            if key in seen and not shot.get("diversity_justification"):
                raise CoveragePlanDiversityError(
                    episode_id=episode_id,
                    beat_id=beat_id,
                    shot_id_a=seen[key],
                    shot_id_b=shot.get("shot_id"),
                )
            seen[key] = shot.get("shot_id")
