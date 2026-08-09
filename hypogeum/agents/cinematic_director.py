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

# regenerate_cinematic_coverage_plan()'s own output-token ceiling
# (2026-08-08, raised 16,000 -> 24,000 same day). Fixed, non-streaming,
# single-call by design (see that function's docstring for why: no
# streaming, no per-batch planning). 16,000 was an operational budget sized
# for the packed code1/code2 wire format, not a canonical architecture
# constraint; it does not fit the stricter, individually-schema-enforced
# slot-key structured-output format below (measured representative 130-shot
# response: ~19,259 estimated tokens). 24,000 leaves ~4,741 tokens of
# estimated margin. Verified (non-inference OpenRouter endpoint metadata,
# 2026-08-08) that every structured-outputs-capable route behind
# anthropic/claude-sonnet-4.6 (Anthropic native x2, Amazon Bedrock x2,
# Azure) declares max_completion_tokens=128,000 -- comfortably above this
# ceiling on every route provider.require_parameters=true could ever select.
_MAX_TOKENS_REGEN = 24_000

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


# ---------------------------------------------------------------------------
# Fail-closed narration completeness gate (2026-08-08)
#
# Canonical fresh production (hypogeum.pipeline.run_pipeline()) must never
# reach run_cinematic_director() unless every authoritative beat has exactly
# one valid measured narration duration -- see that module's call to
# validate_narration_completeness() immediately after Audio Agent completes,
# before Visual Package Director starts. Deliberately NOT enforced inside
# run_cinematic_director() or run_visual_package_director() themselves: both
# functions' own docstrings document that a caller omitting measured
# durations (existing tests, run_visual_package_director_from_plan()'s
# resume-only path, any other legacy/direct caller) intentionally falls back
# to estimate_beat_duration_seconds()'s word-count estimate, and that
# fallback must keep working for those callers. This module adds a single,
# separate, opt-in boundary that the canonical pipeline calls explicitly --
# it never changes what run_cinematic_director() itself does.
# ---------------------------------------------------------------------------

class NarrationCompletenessError(Exception):
    """
    Raised by validate_narration_completeness() when narration_package does
    not supply exactly one valid measured duration_seconds for every
    authoritative beat_id in script_package. See the module-level comment
    above for why this is a separate, opt-in gate rather than a change to
    run_cinematic_director() itself.
    """


def authoritative_beat_ids(script_package: dict) -> list[str]:
    """
    Every beat_id from script_package's sections + shorts_scripts, in
    original document order -- the single source of truth for "what beats
    does this episode actually have" that validate_narration_completeness()
    (and hypogeum.agents.cinematic_director.regenerate_cinematic_coverage_
    plan()) compare narration_package against. Mirrors visual_package_
    director.py's own _flatten_beats_full_text_by_id() beat enumeration
    exactly (same two containers, same order) without importing that module
    (avoids a cross-agent import edge for a two-line loop).
    """
    ids: list[str] = []
    for sec in script_package.get("sections", []):
        for beat in sec.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                ids.append(beat_id)
    for sh in script_package.get("shorts_scripts", []):
        for beat in sh.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                ids.append(beat_id)
    return ids


def authoritative_beat_texts(script_package: dict) -> dict:
    """
    {beat_id: full narration text} for every authoritative beat -- the same
    full-fidelity text visual_package_director.py's own _flatten_beats_
    full_text_by_id() supplies to Cinematic Director's word-count fallback
    (deliberately not the 20-word-capped A2 compaction; see that function's
    own docstring for why the two must stay independent).
    """
    texts: dict = {}
    for sec in script_package.get("sections", []):
        for beat in sec.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                texts[beat_id] = beat.get("text", "")
    for sh in script_package.get("shorts_scripts", []):
        for beat in sh.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                texts[beat_id] = beat.get("text", "")
    return texts


def _is_valid_measured_duration(value: object) -> bool:
    """
    True only for a real, usable positive duration in seconds.

    bool is checked first because Python's bool is an int subclass --
    isinstance(True, (int, float)) is True -- so an unguarded numeric check
    would silently accept True/False as 1.0/0.0. Also rejects any
    non-numeric type, NaN, infinity, zero, and negative values. Never
    coerces an invalid value to zero -- callers reject it instead.
    """
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return False
    return value > 0


def validate_narration_completeness(
    script_package: dict,
    narration_package: Optional[dict],
) -> dict:
    """
    Fail-closed gate: raise NarrationCompletenessError unless
    narration_package supplies exactly one valid measured duration_seconds
    for every authoritative beat_id in script_package. Returns
    {beat_id: duration_seconds} for every authoritative beat on success --
    the exact measured-duration mapping callers need for Cinematic Coverage
    Planning, so a caller that validates first never has to re-derive it.

    Must be called by canonical fresh production BEFORE run_cinematic_
    director() is ever invoked -- see hypogeum.pipeline.run_pipeline()'s
    call immediately after Audio Agent completes. See the module-level
    comment above for why this is not enforced inside run_cinematic_
    director() or run_visual_package_director() themselves.

    Rejects (never coerces to zero or otherwise silently repairs):
      - narration_package absent (None or empty)
      - script_package with no authoritative beats at all
      - a narration_segments entry with a missing/empty beat_reference
      - a beat_reference not present in script_package's authoritative beats
      - more than one narration_segments entry for the same beat_reference
      - a beat_reference whose duration_seconds is missing, zero, negative,
        NaN, infinite, a bool, or any other non-numeric value
      - any authoritative beat_id with no corresponding valid segment at all
    """
    authoritative = authoritative_beat_ids(script_package)
    if not authoritative:
        raise NarrationCompletenessError(
            "script_package supplies no authoritative beat_ids -- nothing to validate"
        )
    authoritative_set = set(authoritative)

    if not narration_package:
        raise NarrationCompletenessError(
            f"narration_package is absent -- {len(authoritative)} authoritative "
            "beats have no measured narration duration available"
        )

    segments = narration_package.get("narration_segments", [])

    valid_durations: dict = {}
    seen_beat_ids: set = set()
    missing_beat_reference = 0
    unmatched_beat_references: list = []
    duplicate_beat_references: list = []
    invalid_durations: list = []

    for seg in segments:
        beat_id = seg.get("beat_reference")
        if not beat_id:
            missing_beat_reference += 1
            continue
        if beat_id not in authoritative_set:
            unmatched_beat_references.append(beat_id)
            continue
        if beat_id in seen_beat_ids:
            duplicate_beat_references.append(beat_id)
            continue
        seen_beat_ids.add(beat_id)
        duration = seg.get("duration_seconds")
        if not _is_valid_measured_duration(duration):
            invalid_durations.append((beat_id, duration))
            continue
        valid_durations[beat_id] = float(duration)

    missing_beats = [b for b in authoritative if b not in valid_durations]

    if (
        missing_beat_reference or unmatched_beat_references
        or duplicate_beat_references or invalid_durations or missing_beats
    ):
        raise NarrationCompletenessError(
            "narration_package does not supply exactly one valid measured "
            f"duration for every authoritative beat "
            f"({len(valid_durations)}/{len(authoritative)} valid). "
            f"segments_missing_beat_reference={missing_beat_reference}, "
            f"unmatched_beat_references={unmatched_beat_references}, "
            f"duplicate_beat_references={duplicate_beat_references}, "
            f"invalid_durations={invalid_durations}, "
            f"beats_with_no_valid_segment={missing_beats}"
        )

    return valid_durations


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


# ---------------------------------------------------------------------------
# Isolated planning-only regeneration (2026-08-08)
#
# regenerate_cinematic_coverage_plan() is the narrowest existing callable
# around run_cinematic_director()'s own generation logic that lets an
# operator deliberately regenerate an EXISTING Cinematic Coverage Plan under
# full validation, backup, and atomic-replace discipline -- without ever
# reaching Shot Diversity Planner, Stage B, any MUAPI/provider call, DB
# write, Assembly, or Publishing. It is intentionally NOT a call to
# run_visual_package_director() or run_cinematic_director() themselves:
# neither of those stops after Cinematic Coverage Planning (VPD continues
# straight into Shot Diversity Planner/Stage B/MUAPI; run_cinematic_
# director() writes directly to the canonical checkpoint with no backup, no
# candidate/temp staging, and falls back to a pass-through plan on LLM
# failure rather than preserving the prior plan). This function is fully
# additive -- it does not modify run_cinematic_director() or any of its
# existing callers/tests, and normal checkpoint-resume behavior (calling
# run_cinematic_director() itself) is completely unchanged by its presence.
#
# It never touches existing generated clips or the asset_manifest -- see
# the module's own README/commit note: resolving what happens to the 88
# already-generated clips when a plan changes is an explicitly separate,
# not-yet-solved problem this function does not attempt.
# ---------------------------------------------------------------------------

class CoverageRegenerationError(Exception):
    """
    Raised by regenerate_cinematic_coverage_plan() for a caller-error/misuse
    condition -- currently only "force=False and no existing checkpoint to
    resume" (this entrypoint never generates a plan implicitly). Genuine
    regeneration-attempt failures (LLM failure, malformed output, arithmetic
    mismatch, fixture-verification mismatch) are NOT raised as exceptions --
    see the function's own docstring for why they are reported in the
    returned result dict instead, with the active plan left untouched.
    """


def _classify_authoritative_beats(script_package: dict) -> dict:
    """{beat_id: "main" | "short"} for every authoritative beat -- main
    beats come from script_package["sections"], short beats from
    script_package["shorts_scripts"]. Used only for the optional, caller-
    supplied fixture_verification totals (main_shots/short_shots) --
    regenerate_cinematic_coverage_plan() itself carries no hardcoded
    per-episode numbers."""
    classification: dict = {}
    for sec in script_package.get("sections", []):
        for beat in sec.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                classification[beat_id] = "main"
    for sh in script_package.get("shorts_scripts", []):
        for beat in sh.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                classification[beat_id] = "short"
    return classification


# ---------------------------------------------------------------------------
# Compact wire format for regenerate_cinematic_coverage_plan() (2026-08-08,
# split into two position-explicit parts 2026-08-08 same day)
#
# A full 130-shot/47-beat cinematic_coverage_plan response (the shape
# run_cinematic_director() itself uses, and the only shape the SYSTEM_PROMPT's
# own OUTPUT FORMAT section requests) measures well past a 16,000-token
# non-streaming budget. Traced against the actual consumers before compacting
# anything (hypogeum.video_prompt_writer._render_shot_directive(), the sole
# Stage-B reader of shot fields; shot_diversity_key() / SHOT_DIVERSITY_FIELDS;
# hypogeum.schemas._validate_cinematic_coverage_plan()'s _req_* calls;
# clip_repair_agent.py's planned_shot_id/shot_order bookkeeping): four
# categories of bytes were pure waste, and none of them are semantic content
# -- reducing authored verbosity, never authored capability:
#
#   1. Values fully DERIVABLE after parsing, never authored by the LLM here:
#      shot_id/parent_beat_id/shot_order (mechanical function of beat_id +
#      position in the shots array), estimated_usable_duration (always the
#      provider's own requested clip duration -- already computed before the
#      call), transition_in/transition_out (hardcoded "clean_cut" by channel
#      policy, never actually a per-shot decision even in the full format),
#      and each beat's narration_duration_seconds (the already-validated
#      measured duration, known before the call).
#   2. Fields no consumer reads at all: foreground_usage, eye_line -- absent
#      from _render_shot_directive()'s reads, absent from
#      SHOT_DIVERSITY_FIELDS, and not required by the schema's _req_* list.
#      Dropped entirely, not restored.
#   3. Free-text field NAMES shortened to short aliases (func, subj, act,
#      fram, light, dup) -- restored to the exact full key names on
#      expansion. The free-text VALUES themselves (narrative_function,
#      subject, action, framing, lighting_direction, diversity_justification)
#      are untouched -- these are genuine authored content, never compacted.
#   4. Enum-valued fields (shot_purpose, shot_scale, camera_angle,
#      camera_height, camera_direction, camera_movement, composition,
#      background_depth, screen_direction, motion_energy) packed into TWO
#      explicitly-keyed 5-character strings ("code1" = positions 1-5,
#      "code2" = positions 6-10 of _COMPACT_CODE_FIELDS -- see
#      _COMPACT_CODE_PARTS), one letter per field, in place of ten separate
#      JSON key/value pairs. Each enum has a small, closed vocabulary
#      (3-12 values) already defined in SYSTEM_PROMPT -- the code is a
#      lossless index into that same vocabulary, decoded via exact lookup in
#      _COMPACT_ENUM_VOCAB immediately after parsing. This is still the
#      single largest reduction versus the full per-field schema: JSON
#      key-name repetition times 10 fields times 130 shots was the dominant
#      cost even after (1)-(3).
#
#      Originally this was ONE 10-character "codes" string. A live
#      production response (ep_f3619022, 2026-08-08) returned a 9-character
#      value for one shot -- the model silently dropped a character
#      somewhere in an unlabeled 10-letter blob, and there is no principled
#      way to know which position was lost or to recover it: shifting the
#      remaining letters, padding with a default, or inferring the missing
#      letter from context would all be guesses about semantic content
#      (a camera angle, a composition choice) that this module must never
#      author on the LLM's behalf. Splitting into two independently-keyed,
#      independently-length-checked 5-character parts does not prevent the
#      model from dropping a letter, but it makes a drop unambiguous and
#      cheaply detectable at each half's own boundary (the malformed half is
#      always exactly identifiable), and it is validated exactly the same
#      way: any part that is not exactly 5 characters, or contains any
#      out-of-range letter, fails expansion with ValueError -- reject and
#      preserve, never repair.
#
#      lens_character (optional) and progression_pattern (per-beat) remain
#      single-letter codes against their own small vocabularies -- neither
#      is part of the two 5-character packed strings.
#
# Every field _render_shot_directive(), shot_diversity_key(), and the schema
# actually require is still authored by the LLM (as free text or as an
# unambiguous code) and still ends up, verbatim, in the expanded candidate
# that gets schema-validated before install -- nothing semantic is lost,
# only the redundant/derivable/unread/over-verbose bytes are. An invalid,
# missing, short, or long code part fails expansion with ValueError, which
# the caller treats exactly like a schema-validation failure (reject,
# preserve the active plan) -- compaction never weakens the fail-closed
# contract, and expansion never guesses a missing or malformed value.
# ---------------------------------------------------------------------------

# Fixed field order for the two packed code parts -- position i's letter
# decodes against _COMPACT_ENUM_VOCAB[_COMPACT_CODE_FIELDS[i]]. This order
# is the single source of truth for both parts; it is never reordered or
# altered by the split below -- only how it is chunked into JSON keys
# changed.
_COMPACT_CODE_FIELDS: list = [
    "shot_purpose", "shot_scale", "camera_angle", "camera_height",
    "camera_direction", "camera_movement", "composition", "background_depth",
    "screen_direction", "motion_energy",
]

# {part_key: [fields]} -- "code1" carries positions 1-5 of
# _COMPACT_CODE_FIELDS, "code2" carries positions 6-10. Each part is always
# exactly 5 characters; the split point is fixed at the midpoint of the same
# unchanged 10-field order above, so no enum's meaning or position changes.
_COMPACT_CODE_PARTS: dict = {
    "code1": _COMPACT_CODE_FIELDS[:5],
    "code2": _COMPACT_CODE_FIELDS[5:],
}

# Canonical vocabulary for every enum-coded field -- single source of truth
# for both the codes embedded in _COMPACT_OUTPUT_INSTRUCTIONS and the
# decoding in _decode_enum_code(). Values and order match
# hypogeum/prompts/cinematic_director.txt's own PER-SHOT FIELDS /
# PROGRESSION PATTERNS vocabulary exactly -- this module never invents a
# value SYSTEM_PROMPT doesn't already teach.
_COMPACT_ENUM_VOCAB: dict = {
    "shot_purpose": [
        "establish_location", "character_emotion", "object_detail",
        "action_beat", "transition_beat", "reaction",
    ],
    "shot_scale": [
        "extreme_close_up", "close_up", "medium_close_up", "medium",
        "medium_wide", "wide_establishing", "overhead", "macro", "low_angle",
    ],
    "camera_angle": [
        "eye_level", "low_angle", "high_angle", "overhead", "dutch_tilt", "worms_eye",
    ],
    "camera_height": [
        "ground_level", "knee_height", "eye_level", "elevated", "overhead",
    ],
    "camera_direction": [
        "frontal", "three_quarter", "profile", "rear", "overhead_down",
    ],
    "camera_movement": [
        "slow_push_in", "controlled_pull_back", "lateral_dolly_left",
        "lateral_dolly_right", "vertical_descent", "vertical_ascent",
        "orbital_arc", "overhead_descent", "handheld_drift",
        "locked_off_hold", "slow_tilt_up", "slow_tilt_down",
    ],
    "composition": [
        "rule_of_thirds_left", "rule_of_thirds_right", "centered_symmetry",
        "diagonal_leading_line", "foreground_frame", "layered_depth",
        "negative_space_dominant", "edge_of_frame_subject",
    ],
    "background_depth": ["shallow", "medium", "deep_layered"],
    "screen_direction": [
        "left_to_right", "right_to_left", "neutral", "toward_camera", "away_from_camera",
    ],
    "motion_energy": ["low", "medium", "high"],
    "lens_character": ["standard", "wide", "macro", "telephoto_compression"],
    "progression_pattern": list(PROGRESSION_PATTERNS),
}

# {compact_key: full_schema_key} for the free-text (non-enum-coded) shot
# fields -- values pass through unchanged, only the JSON key name expands.
_COMPACT_FREE_TEXT_FIELDS: dict = {
    "func": "narrative_function",
    "subj": "subject",
    "act": "action",
    "fram": "framing",
    "light": "lighting_direction",
}


def _format_enum_code_legend(field: str) -> str:
    """One line of 'A=value B=value ...' for `field`'s vocabulary, used to
    build _COMPACT_OUTPUT_INSTRUCTIONS from _COMPACT_ENUM_VOCAB -- single
    source of truth, the instructions text can never drift from the actual
    decode table."""
    vocab = _COMPACT_ENUM_VOCAB[field]
    return " ".join(f"{chr(65 + i)}={value}" for i, value in enumerate(vocab))


def _decode_enum_code(field: str, code: object) -> str:
    """Decode one single-letter code against _COMPACT_ENUM_VOCAB[field].
    Raises ValueError on anything but an exact, in-range letter -- an
    invalid/unknown code is a malformed-output condition, not something
    this function silently repairs."""
    vocab = _COMPACT_ENUM_VOCAB[field]
    if not isinstance(code, str) or len(code) != 1:
        raise ValueError(f"{field} code must be a single letter, got {code!r}")
    index = ord(code.upper()) - 65
    if index < 0 or index >= len(vocab):
        raise ValueError(
            f"{field} code {code!r} out of range (vocabulary has {len(vocab)} values)"
        )
    return vocab[index]


def _decode_code_part(part_key: str, value: object) -> dict:
    """Decode one 5-character packed code part (part_key is 'code1' or
    'code2') into {full_schema_key: value} for the five enum-coded shot
    fields _COMPACT_CODE_PARTS[part_key] assigns to that part.

    Raises ValueError if `value` is not a string, is not exactly 5
    characters, or contains any out-of-range letter -- a missing, short,
    long, or invalid part is a malformed-output condition. Never shifts,
    pads, defaults, or infers a missing character: an ambiguous or
    incomplete part is rejected whole, exactly like any other
    schema-validation failure."""
    fields = _COMPACT_CODE_PARTS[part_key]
    if not isinstance(value, str) or len(value) != len(fields):
        raise ValueError(
            f"{part_key!r} must be a {len(fields)}-character string, got {value!r}"
        )
    return {
        field: _decode_enum_code(field, letter)
        for field, letter in zip(fields, value)
    }


def _build_compact_output_instructions() -> str:
    """Build the full COMPACT OUTPUT OVERRIDE instruction text from
    _COMPACT_ENUM_VOCAB/_COMPACT_CODE_PARTS -- computed once at import time
    (see _COMPACT_OUTPUT_INSTRUCTIONS below) so the instructions the LLM
    receives can never drift from what _decode_code_part() actually decodes.
    This text goes in the INPUT (director_input, not the output), so its
    length never counts against the 16,000-token output ceiling -- only
    per-shot OUTPUT verbosity does, which is what the codes/aliases below
    actually reduce."""
    part_legends = []
    for part_key, fields in _COMPACT_CODE_PARTS.items():
        offset = _COMPACT_CODE_FIELDS.index(fields[0])
        lines = "\n    ".join(
            f"position {i + 1} - {field}: {_format_enum_code_legend(field)}"
            for i, field in enumerate(fields, start=offset)
        )
        part_legends.append(f'"{part_key}" (fields {offset + 1}-{offset + len(fields)}):\n    {lines}')
    codes_legend = "\n\n  ".join(part_legends)

    example_parts = ", ".join(
        f'"{part_key}": "{"".join(chr(65) for _ in fields)}"'
        for part_key, fields in _COMPACT_CODE_PARTS.items()
    )

    return f"""COMPACT OUTPUT OVERRIDE (supersedes the OUTPUT FORMAT section above for this task only):

Respond with ONLY this compact JSON -- no prose, no markdown, no code fences. Do not include
shot_id, parent_beat_id, shot_order, estimated_usable_duration, transition_in, transition_out,
foreground_usage, eye_line, or narration_duration_seconds anywhere -- all of those are derived
programmatically after parsing and any tokens spent on them are wasted.

{{
  "beats": [
    {{
      "beat_id": "<beat_id, exactly one entry per input beat, same order as the input beats list>",
      "pattern": "<single letter -- see PATTERN CODE below>",
      "shots": [
        {{
          "code1": "<exactly 5 letters, one per position below, no separators>",
          "code2": "<exactly 5 letters, one per position below, no separators>",
          "func": "<narrative_function -- one sentence>",
          "subj": "<subject, terse>",
          "act": "<action, terse -- omit this key entirely if static>",
          "fram": "<framing, terse -- omit if not distinct from subject>",
          "light": "<lighting_direction, terse -- omit if none>",
          "lens": "<single letter -- see LENS CODE below; omit entirely if standard>",
          "dup": "<diversity_justification reason -- ONLY when intentionally repeating a sibling's attributes; omit this key entirely otherwise>"
        }}
      ]
    }}
  ]
}}

CODE PARTS -- "code1" and "code2" are each EXACTLY 5 letters, no separators. Never combine them
into one string, never omit either key, never send more or fewer than 5 letters in either part:
  {codes_legend}

Example: {example_parts} would mean
{", ".join(f"{field}={_COMPACT_ENUM_VOCAB[field][0]}" for field in _COMPACT_CODE_FIELDS)}.

PATTERN CODE (per beat, one letter): {_format_enum_code_legend("progression_pattern")}

LENS CODE (optional, one letter): {_format_enum_code_legend("lens_character")}

shots[] length must be >=1 and <= that beat's shot_budget. Every code must decode to one of the
exact vocabulary values already defined earlier in these instructions -- the legends above are
that same vocabulary, just indexed by letter. Omit an optional key entirely rather than sending
"" or null for it. "code1" and "code2" are both mandatory on every shot."""


_COMPACT_OUTPUT_INSTRUCTIONS = _build_compact_output_instructions()



def _expand_compact_plan(
    compact: dict,
    episode_id: str,
    authoritative: list,
    measured_durations: dict,
    provider_clip_duration: float,
) -> dict:
    """
    Expand a compact-wire-format LLM response into the exact full canonical
    cinematic_coverage_plan shape hypogeum.schemas._validate_cinematic_
    coverage_plan() requires and every existing Stage B consumer already
    reads (hypogeum.video_prompt_writer._render_shot_directive(),
    shot_diversity_key(), clip_repair_agent.py's planned_shot_id
    bookkeeping) -- restoring shot_id/parent_beat_id/shot_order (from beat_id
    + array position), estimated_usable_duration (the provider's own
    requested clip duration), transition_in/transition_out (always
    "clean_cut" -- fixed channel policy, not a per-shot LLM decision even in
    the full format), each beat's narration_duration_seconds (the
    already-validated measured duration), every enum-coded field (decoded
    via _decode_code_part()/_decode_enum_code() against _COMPACT_CODE_PARTS/
    _COMPACT_ENUM_VOCAB), and every free-text field (renamed via
    _COMPACT_FREE_TEXT_FIELDS, values untouched). The returned dict is
    byte-for-byte the same shape run_cinematic_director() itself produces;
    nothing downstream can tell the two apart.

    Raises ValueError on any structural problem (wrong beat count, unknown
    or duplicate beat_id, a beat with no shots, a malformed or out-of-range
    code) -- the caller treats this identically to a schema-validation
    failure: reject the candidate, preserve the active plan, stop cleanly.
    """
    compact_beats = compact.get("beats")
    if not isinstance(compact_beats, list):
        raise ValueError("compact response missing a 'beats' list")
    if len(compact_beats) != len(authoritative):
        raise ValueError(
            f"compact response has {len(compact_beats)} beats, expected {len(authoritative)}"
        )

    authoritative_set = set(authoritative)
    seen_beat_ids: set = set()
    beat_plans = []
    for cb in compact_beats:
        if not isinstance(cb, dict):
            raise ValueError("each compact beat entry must be a dict")
        beat_id = cb.get("beat_id")
        if beat_id not in authoritative_set:
            raise ValueError(f"compact beat_id {beat_id!r} is not an authoritative beat")
        if beat_id in seen_beat_ids:
            raise ValueError(f"duplicate compact beat_id {beat_id!r}")
        seen_beat_ids.add(beat_id)

        compact_shots = cb.get("shots")
        if not isinstance(compact_shots, list) or not compact_shots:
            raise ValueError(f"beat {beat_id!r} has no shots")

        progression_pattern = _decode_enum_code("progression_pattern", cb.get("pattern"))

        shots = []
        for i, cs in enumerate(compact_shots, start=1):
            if not isinstance(cs, dict):
                raise ValueError(f"beat {beat_id!r} shot {i} is not a dict")
            shot = {
                "shot_id": f"{beat_id}__shot{i}",
                "parent_beat_id": beat_id,
                "shot_order": i,
                "estimated_usable_duration": provider_clip_duration,
                "transition_in": "clean_cut",
                "transition_out": "clean_cut",
                "diversity_justification": cs.get("dup") or None,
            }
            shot.update(_decode_code_part("code1", cs.get("code1")))
            shot.update(_decode_code_part("code2", cs.get("code2")))
            for compact_key, full_key in _COMPACT_FREE_TEXT_FIELDS.items():
                value = cs.get(compact_key)
                if value not in (None, ""):
                    shot[full_key] = value
            lens_code = cs.get("lens")
            if lens_code:
                shot["lens_character"] = _decode_enum_code("lens_character", lens_code)
            shots.append(shot)

        beat_plans.append({
            "beat_id": beat_id,
            "narration_duration_seconds": round(measured_durations[beat_id], 3),
            "progression_pattern": progression_pattern,
            "shots": shots,
        })

    missing = authoritative_set - seen_beat_ids
    if missing:
        raise ValueError(f"compact response missing beats: {sorted(missing)}")

    return {"episode_id": episode_id, "beat_plans": beat_plans}


# ---------------------------------------------------------------------------
# Structured-output (strict json_schema) wire format for
# regenerate_cinematic_coverage_plan() (2026-08-08)
#
# Three consecutive live isolated-planning attempts against the prompt-only
# compact format above each failed differently and safely (a dropped
# packed-code character, an under-count shot array for one beat, an
# out-of-vocabulary enum letter) -- proving prompt instructions plus
# post-response validation alone are not reliable enough for this
# high-cardinality artifact. This section constrains generation itself via
# OpenRouter/Anthropic strict structured outputs (response_format:
# {"type": "json_schema", "json_schema": {"strict": true, ...}}), verified
# against Anthropic's own documented JSON Schema subset
# (platform.claude.com/docs/en/build-with-claude/structured-outputs,
# fetched live 2026-08-08) before any of this was written:
#
#   Supported:     object, array, string, integer, number, boolean, null;
#                   enum (strings/numbers/bools/null only); const; anyOf/
#                   allOf (limited); internal $ref/$def/definitions;
#                   default; required; additionalProperties (must be
#                   false); string formats; array minItems (0 or 1 only).
#   Not supported:  array constraints beyond minItems of 0/1 (no maxItems,
#                   no exact length at all), external $ref, recursive
#                   schemas, numeric/string length constraints.
#
# Arrays therefore CANNOT enforce "beat b_014 has exactly 5 shots" -- there
# is no maxItems and no minItems above 1. This module never represents
# shots as an array. Instead, each beat's shots are an OBJECT with
# deterministic slot keys ("s01".."sNN", exactly the beat's authoritative
# shot budget), every slot key REQUIRED, additionalProperties: false. Since
# JSON Schema requires every declared property to be present and forbids
# any other property, this makes an incorrect shot count for beats
# structurally unsatisfiable -- not merely instructed against -- using only
# the confirmed-supported keywords above (object/properties/required/
# additionalProperties/string/enum). The same technique makes the 47
# authoritative beats a required, closed set of named top-level properties
# (never an array), and makes each of the ten enum-coded shot fields (see
# _COMPACT_CODE_FIELDS) its own required single-letter `enum`-constrained
# property -- e.g. background_depth's property can only ever be "A", "B",
# or "C"; "F" becomes unsatisfiable at the API level, not merely rejected
# after the fact.
#
# The response this produces is a NEW, richer wire shape (slot-keyed
# objects, one property per enum position) than the packed code1/code2
# strings above. _structured_payload_to_compact() converts it, by pure
# reshaping with no guessing or repair, into the exact same compact dict
# shape (code1/code2/func/subj/act/fram/light/lens/dup) the prompt-only
# path already produces -- so _expand_compact_plan() below runs completely
# unmodified on the result, and the installed canonical plan's shape is
# unaffected by any of this. This is a provider-facing wire-format change
# only; every existing arithmetic/schema/fixture/atomic-install check still
# runs, unchanged, on the expanded candidate.
# ---------------------------------------------------------------------------

# {full_schema_key: short structured-output property name} for the ten
# enum-coded shot fields, in the same fixed order as _COMPACT_CODE_FIELDS.
# Kept short because — unlike the schema itself, which is request-side and
# does not count against the 16,000-token output ceiling — every property
# NAME in the actual generated response is echoed back and does count.
_STRUCTURED_ENUM_PROPERTY_NAMES: dict = {
    "shot_purpose":     "pur",
    "shot_scale":       "scl",
    "camera_angle":     "ang",
    "camera_height":    "hgt",
    "camera_direction": "dir",
    "camera_movement":  "mov",
    "composition":      "cmp",
    "background_depth": "dep",
    "screen_direction":  "scr",
    "motion_energy":    "nrg",
}

# Reverse lookup used by the converter below.
_STRUCTURED_PROPERTY_TO_FIELD: dict = {
    prop: field for field, prop in _STRUCTURED_ENUM_PROPERTY_NAMES.items()
}

# OpenRouter provider-routing filter for the structured-output request.
# require_parameters: true tells OpenRouter to route only to endpoints that
# actually support every parameter the request sends (here: response_format
# with a json_schema payload) -- of the 7 OpenRouter endpoints behind
# anthropic/claude-sonnet-4.6 (verified live 2026-08-08), the two Google
# Vertex routes do not declare structured_outputs support and are excluded
# by this filter automatically; the other 5 remain eligible, preserving
# OpenRouter's normal load-balancing/fallback across all of them. No
# provider is pinned, ordered, allow-listed, or deny-listed -- this is
# strictly a capability filter, not a routing preference.
_STRUCTURED_OUTPUT_PROVIDER: dict = {"require_parameters": True}


def _structured_shot_schema() -> dict:
    """JSON Schema (Anthropic strict-structured-outputs subset only) for one
    shot: the ten enum-coded fields as individually-constrained single-
    letter string properties (via _STRUCTURED_ENUM_PROPERTY_NAMES), plus the
    same free-text/lens/dup fields the compact format already carries.
    Strict mode has no concept of an optional property -- every property
    below is required, and the free-text fields use "" as the explicit
    "not applicable" sentinel, which _expand_compact_plan()'s existing
    `if value not in (None, ""):` check already treats identically to an
    omitted key. `lens` is always present too (default "A" = standard),
    which only ever adds an always-populated, schema-optional
    lens_character to the expanded shot -- never required downstream, so
    this is a strictly additive, harmless change from the prompt-only
    path's "omit if standard" convention."""
    properties: dict = {}
    required: list = []
    for field in _COMPACT_CODE_FIELDS:
        prop = _STRUCTURED_ENUM_PROPERTY_NAMES[field]
        vocab = _COMPACT_ENUM_VOCAB[field]
        properties[prop] = {
            "type": "string",
            "enum": [chr(65 + i) for i in range(len(vocab))],
            "description": _format_enum_code_legend(field),
        }
        required.append(prop)

    free_text_descriptions = {
        "func": "narrative_function -- one sentence.",
        "subj": "subject, terse.",
        "act": "action, terse. Use \"\" if the shot is static.",
        "fram": "framing, terse. Use \"\" if not distinct from subject.",
        "light": "lighting_direction, terse. Use \"\" if none.",
        "dup": (
            "diversity_justification -- ONLY when intentionally repeating a "
            "sibling shot's camera/composition attributes. Use \"\" otherwise."
        ),
    }
    for prop, description in free_text_descriptions.items():
        properties[prop] = {"type": "string", "description": description}
        required.append(prop)

    lens_vocab = _COMPACT_ENUM_VOCAB["lens_character"]
    properties["lens"] = {
        "type": "string",
        "enum": [chr(65 + i) for i in range(len(lens_vocab))],
        "description": (
            _format_enum_code_legend("lens_character")
            + " -- use \"A\" (standard) unless a specific lens character is intentional."
        ),
    }
    required.append("lens")

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STRUCTURED_SHOT_REF = {"$ref": "#/$defs/shot"}


def _structured_shots_schema(n_shots: int) -> dict:
    """Object schema for one beat's shots: deterministic slot keys
    "s01".."s{n_shots:02d}", every slot REQUIRED, additionalProperties:
    False. No array, no minItems/maxItems -- exact cardinality is a direct
    structural consequence of "every declared property is required and no
    other property is allowed", both confirmed-supported keywords. Every
    slot references the single shared "#/$defs/shot" definition (internal
    $ref/$defs is confirmed supported by Anthropic's documented strict-mode
    subset) rather than repeating the ~17-property shot schema inline at
    each of up to 130 slots."""
    slot_keys = [f"s{i:02d}" for i in range(1, n_shots + 1)]
    return {
        "type": "object",
        "properties": {key: _STRUCTURED_SHOT_REF for key in slot_keys},
        "required": slot_keys,
        "additionalProperties": False,
    }


def _structured_beat_schema(n_shots: int) -> dict:
    pattern_vocab = _COMPACT_ENUM_VOCAB["progression_pattern"]
    return {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "enum": [chr(65 + i) for i in range(len(pattern_vocab))],
                "description": _format_enum_code_legend("progression_pattern"),
            },
            "shots": _structured_shots_schema(n_shots),
        },
        "required": ["pattern", "shots"],
        "additionalProperties": False,
    }


def build_structured_coverage_plan_schema(
    authoritative_order: list,
    shot_budget_by_beat_id: dict,
) -> dict:
    """Top-level JSON Schema for the structured-output Coverage Plan
    request: an object whose properties are exactly the 47 authoritative
    beat_ids (declared, in authoritative order, in `properties`; all listed
    in `required`; additionalProperties: False), each beat carrying its own
    exact per-beat shot-slot schema from shot_budget_by_beat_id. No array is
    used anywhere in this schema -- see the module comment above for why.
    The single shot schema lives once in $defs.shot; every shot slot at
    every beat references it via "#/$defs/shot" instead of repeating it."""
    properties: dict = {}
    for beat_id in authoritative_order:
        properties[beat_id] = _structured_beat_schema(shot_budget_by_beat_id[beat_id])
    return {
        "$defs": {"shot": _structured_shot_schema()},
        "type": "object",
        "properties": properties,
        "required": list(authoritative_order),
        "additionalProperties": False,
    }


def _build_structured_response_format(schema: dict, name: str = "cinematic_coverage_plan") -> dict:
    """`name` defaults to the single-request schema name (unused by any
    caller after the 2026-08-09 batching correction, kept as the default so
    this function's own signature stays backward-compatible) -- the batch
    loop below always passes its own deterministic, unique
    "cinematic_coverage_plan_batch_{index:02d}" name instead."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def _no_duplicate_keys_object_pairs_hook(pairs: list) -> dict:
    """json.loads(object_pairs_hook=...) hook that raises ValueError on any
    repeated key within the same JSON object, instead of silently keeping
    only the last occurrence (the standard dict-building behavior). A
    duplicate key is malformed output -- caught here, before it can ever
    silently collapse into a single value during expansion."""
    seen: set = set()
    result: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r} in structured response")
        seen.add(key)
        result[key] = value
    return result


def _structured_payload_to_compact(
    structured: dict,
    authoritative_order: list,
    shot_budget_by_beat_id: dict,
) -> dict:
    """
    Reshape a structured-output (slot-keyed, per-field-enum) response into
    the exact compact dict shape _expand_compact_plan() already consumes
    (code1/code2/func/subj/act/fram/light/lens/dup per shot, a plain list of
    {"beat_id", "pattern", "shots"} beat dicts) -- pure reshaping, no
    semantic value is read, altered, guessed, or repaired here.

    Strict structured-output mode is contracted by the provider to make a
    schema-violating response impossible, but this function does not trust
    that contract blindly: it independently re-checks that every
    authoritative beat is present exactly once and that every beat's slot
    keys are exactly "s01".."s{budget:02d}" with no gaps or extras, raising
    ValueError otherwise -- caught by the same malformed_output handling as
    every other failure mode in regenerate_cinematic_coverage_plan().
    _expand_compact_plan() then re-validates every code/value anyway, so
    this is defense in depth, not the only check.
    """
    if not isinstance(structured, dict):
        raise ValueError("structured response is not a JSON object")
    beats_obj = structured.get("beats")
    if not isinstance(beats_obj, dict):
        raise ValueError("structured response missing a 'beats' object")

    compact_beats = []
    for beat_id in authoritative_order:
        beat = beats_obj.get(beat_id)
        if not isinstance(beat, dict):
            raise ValueError(f"structured response missing beat {beat_id!r}")

        shots_obj = beat.get("shots")
        if not isinstance(shots_obj, dict):
            raise ValueError(f"beat {beat_id!r} has no shots object")

        expected_budget = shot_budget_by_beat_id[beat_id]
        expected_slot_keys = [f"s{i:02d}" for i in range(1, expected_budget + 1)]
        if sorted(shots_obj.keys()) != expected_slot_keys:
            raise ValueError(
                f"beat {beat_id!r} has slot keys {sorted(shots_obj.keys())!r}, "
                f"expected exactly {expected_slot_keys!r}"
            )

        compact_shots = []
        for slot_key in expected_slot_keys:
            s = shots_obj[slot_key]
            if not isinstance(s, dict):
                raise ValueError(f"beat {beat_id!r} slot {slot_key!r} is not an object")
            code1 = "".join(
                s.get(_STRUCTURED_ENUM_PROPERTY_NAMES[field], "")
                for field in _COMPACT_CODE_PARTS["code1"]
            )
            code2 = "".join(
                s.get(_STRUCTURED_ENUM_PROPERTY_NAMES[field], "")
                for field in _COMPACT_CODE_PARTS["code2"]
            )
            compact_shot = {"code1": code1, "code2": code2}
            for key in ("func", "subj", "act", "fram", "light", "dup"):
                compact_shot[key] = s.get(key, "")
            compact_shot["lens"] = s.get("lens", "")
            compact_shots.append(compact_shot)

        compact_beats.append({
            "beat_id": beat_id,
            "pattern": beat.get("pattern"),
            "shots": compact_shots,
        })

    return {"beats": compact_beats}


_STRUCTURED_OUTPUT_CONTENT_NOTE = (
    "Respond using exactly the JSON Schema provided via response_format -- structure and "
    "every enum letter are already constrained by that schema; this note only covers "
    "content. Each field's meaning is documented in its own schema property description. "
    "Write genuinely differentiated, specific content for func/subj/act/fram/light on every "
    "shot -- do not reuse the same phrase across shots in a beat."
)

# ---------------------------------------------------------------------------
# Batched strict planning (2026-08-09)
#
# A live isolated-planning attempt against the single 47-beat/130-slot
# structured schema (30,715 bytes, 288 required properties) was rejected by
# Anthropic before generation began: "The compiled grammar is too large,
# which would cause performance issues." This is a runtime grammar-
# compilation cost limit, not a documented JSON-Schema-subset violation --
# every keyword used was already confirmed supported; the schema was simply
# too large in aggregate for the provider's strict-mode constrained-decoding
# engine to compile at 47-beat/130-slot scale. No provider byte threshold
# was published, so _MAX_SLOTS_PER_BATCH below is a conservative structural
# reduction against the rejected 130-slot grammar, not a claimed precise
# limit -- the sole knob to revisit if it ever needs adjusting.
#
# Splitting the SAME schema machinery (same slot-key/per-position-enum
# design, same fail-closed conversion path) into several smaller strict
# requests, each well under the scale that failed, produces one independent
# one-request-maximum call per batch (max_attempts=1,
# allow_model_fallback=False -- the identical contract the single-request
# design already used). Each batch yields its own compact-shape beat list
# via the UNCHANGED _structured_payload_to_compact()/_expand_compact_plan()
# path. Nothing is merged into a canonical plan, nothing is written to disk,
# and no batch result is treated as resumable state until every batch has
# succeeded -- a failure at batch N discards every batch already collected
# in memory and rejects the whole attempt exactly as a single-request
# failure always has (see regenerate_cinematic_coverage_plan()'s own
# docstring). This keeps the atomic-installation contract identical to the
# single-request design: one full-plan validation pass, one atomic
# Path.replace(), or nothing installed at all.
# ---------------------------------------------------------------------------

_MAX_SLOTS_PER_BATCH = 25


def _partition_beats_into_batches(
    authoritative_order: list,
    shot_budget_by_beat_id: dict,
    max_slots_per_batch: int = _MAX_SLOTS_PER_BATCH,
) -> list:
    """Deterministic greedy partition of authoritative_order into batches of
    at most max_slots_per_batch total shot slots each, filling each batch in
    authoritative order until the next complete beat would exceed the
    ceiling. A beat is never split across batches. Returns a list of
    beat_id lists; concatenating them in order reproduces
    authoritative_order exactly, with no overlap and no omission -- and
    every returned batch has at most max_slots_per_batch total slots, with
    no exception.

    Raises ValueError if any single beat's own shot budget exceeds
    max_slots_per_batch -- such a beat could only be represented by
    splitting it across two batches, truncating its budget, or placing it
    alone in a batch that violates the ceiling invariant; none of those are
    acceptable, so this is a hard, deterministic validation failure raised
    before any batch is built and before any provider call becomes
    reachable, exactly like NarrationCompletenessError is for other
    input-validation failures."""
    batches: list = []
    current: list = []
    current_slots = 0
    for beat_id in authoritative_order:
        n = shot_budget_by_beat_id[beat_id]
        if n > max_slots_per_batch:
            raise ValueError(
                f"beat {beat_id!r} has a shot budget of {n}, which exceeds "
                f"max_slots_per_batch={max_slots_per_batch} -- it can never fit "
                "in any batch without violating the batch-size ceiling; refusing "
                "to split, truncate, or place it in an oversized batch"
            )
        if current and current_slots + n > max_slots_per_batch:
            batches.append(current)
            current = []
            current_slots = 0
        current.append(beat_id)
        current_slots += n
    if current:
        batches.append(current)
    return batches


# Deterministic per-batch max_tokens formula, fitted (with a deliberately
# generous safety margin) against the representative structured-response
# measurement used throughout this correction: observed ~144-150 estimated
# tokens per shot slot plus a small fixed per-beat wrapper overhead
# (beat_id key, "pattern" property, JSON nesting) -- both coefficients
# rounded up, plus a flat 2,000-token safety buffer, then rounded up again
# to the nearest 500 for a clean, auditable ceiling. Verified against every
# batch of the real ep_f3619022 47-beat/130-slot partition: minimum margin
# over the measured representative response was 2,295 tokens, maximum
# per-batch ceiling 6,500 tokens -- far within the 128,000-token
# max_completion_tokens every structured-outputs-capable route for this
# model declares (verified via non-inference OpenRouter endpoint metadata).
_CONSERVATIVE_TOKENS_PER_SLOT = 160
_CONSERVATIVE_TOKENS_PER_BEAT = 20
_BATCH_TOKEN_SAFETY_MARGIN = 2_000
_BATCH_TOKEN_ROUNDING = 500


def _estimate_batch_max_tokens(beat_ids: list, shot_budget_by_beat_id: dict) -> int:
    n_slots = sum(shot_budget_by_beat_id[b] for b in beat_ids)
    n_beats = len(beat_ids)
    estimated = (
        n_slots * _CONSERVATIVE_TOKENS_PER_SLOT
        + n_beats * _CONSERVATIVE_TOKENS_PER_BEAT
        + _BATCH_TOKEN_SAFETY_MARGIN
    )
    return ((estimated + _BATCH_TOKEN_ROUNDING - 1) // _BATCH_TOKEN_ROUNDING) * _BATCH_TOKEN_ROUNDING


def _batch_content_note(batch_index: int, batch_count: int) -> str:
    return (
        _STRUCTURED_OUTPUT_CONTENT_NOTE
        + f" This is batch {batch_index} of {batch_count} of a larger sequential "
        "Cinematic Coverage Plan regeneration -- respond with structured content for "
        "ONLY the beats present in this request's schema. Beats outside this batch are "
        "being planned in separate requests and must not be referenced or repeated here."
    )


def _execute_single_batch(
    batch_index: int,
    batch_count: int,
    batch_beat_ids: list,
    expected_shot_budget_by_beat_id: dict,
    measured_durations: dict,
    scenes_by_beat_id: dict,
    visual_package: dict,
    episode_id: str,
    config: Config,
    provider_clip_duration: float,
) -> dict:
    """
    Execute exactly one batch's LLM request through the production batch
    schema, prompt envelope, token budget, response_format, provider, and
    model configuration -- the single shared code path both ordinary
    batched regeneration (calling this once per batch, sequentially) and
    the first-batch feasibility probe (calling this exactly once, for
    batch 1, and never proceeding further) both use. A probe therefore
    genuinely exercises the exact production request construction, never a
    parallel or duplicated implementation of it.

    Makes at most one requests.post call: response_format is strict
    json_schema, provider.require_parameters=true (no pinning),
    allow_model_fallback=False, max_attempts=1 -- see call_claude()'s own
    docstring for what each of those guarantees.

    Never raises. Returns one of:
      {"status": "batch_succeeded", "beat_plans": [...], "result": CallResult}
      {"status": "batch_failed", "reason": "..."}
    """
    from hypogeum.agent import _strip_markdown_fence  # noqa: PLC0415
    from hypogeum.finance import record_cost  # noqa: PLC0415
    from hypogeum.openrouter import call_claude  # noqa: PLC0415

    batch_budget = {b: expected_shot_budget_by_beat_id[b] for b in batch_beat_ids}

    beats_for_director = []
    for beat_id in batch_beat_ids:
        duration = measured_durations[beat_id]
        scene = scenes_by_beat_id[beat_id]
        beats_for_director.append({
            "beat_id": beat_id,
            "scene_id": scene.get("scene_id"),
            "source_type": scene.get("source_type"),
            "narration_duration_seconds": round(duration, 3),
            "shot_budget": batch_budget[beat_id],
            "visual_spec": scene.get("visual_spec", {}),
            "presentation_plan": scene.get("presentation_plan", {}),
            "narrative_signals": scene.get("narrative_signals", []),
        })

    batch_schema = build_structured_coverage_plan_schema(batch_beat_ids, batch_budget)
    batch_max_tokens = _estimate_batch_max_tokens(batch_beat_ids, batch_budget)

    director_input = {
        "task": "plan_cinematic_coverage_structured_batch",
        "episode_metadata": {
            "episode_title": visual_package.get("episode_metadata", {}).get("episode_title", ""),
            "total_beats": len(beats_for_director),
            "episode_id": episode_id,
            "batch_index": batch_index,
            "batch_count": batch_count,
        },
        "progression_patterns": list(PROGRESSION_PATTERNS),
        "beats": beats_for_director,
        "output_format_override": _batch_content_note(batch_index, batch_count),
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(director_input, ensure_ascii=False)},
    ]
    try:
        result = call_claude(
            model_slug=_CLAUDE_SONNET,
            messages=messages,
            max_tokens=batch_max_tokens,
            config=config,
            episode_id=episode_id,
            agent=_AGENT_NAME,
            response_format=_build_structured_response_format(
                batch_schema, name=f"cinematic_coverage_plan_batch_{batch_index:02d}",
            ),
            provider=_STRUCTURED_OUTPUT_PROVIDER,
            allow_model_fallback=False,
            max_attempts=1,
        )
    except Exception as exc:  # noqa: BLE001 — any LLM/network/truncation failure
        return {
            "status": "batch_failed",
            "reason": (
                f"llm_call_failed: batch {batch_index}/{batch_count} "
                f"(beats {batch_beat_ids}): {type(exc).__name__}: {exc}"
            ),
        }

    record_cost(
        provider="openrouter",
        model=result.model_used,
        call_type=_AGENT_NAME,
        cost_usd=result.estimated_cost_usd,
        tokens_or_credits=float(result.usage.get("total_tokens", 0)),
    )

    # Parsed AND fully expanded (decoded, validated) immediately, scoped to
    # just this batch's beats -- every per-shot check _expand_compact_plan()
    # performs (invalid/out-of-range enum letters, missing/duplicate/unknown
    # beats within the batch, empty shots) therefore fails THIS batch
    # immediately, before any later batch could ever be submitted.
    raw_content = _strip_markdown_fence(result.content)
    try:
        structured = json.loads(
            raw_content, object_pairs_hook=_no_duplicate_keys_object_pairs_hook,
        )
        batch_compact = _structured_payload_to_compact(
            structured, batch_beat_ids, batch_budget,
        )
        batch_measured_durations = {b: measured_durations[b] for b in batch_beat_ids}
        batch_expanded = _expand_compact_plan(
            batch_compact, episode_id, batch_beat_ids, batch_measured_durations,
            provider_clip_duration,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "batch_failed",
            "reason": (
                f"malformed_output: batch {batch_index}/{batch_count} "
                f"(beats {batch_beat_ids}): {type(exc).__name__}: {exc}"
            ),
        }

    return {
        "status": "batch_succeeded",
        "beat_plans": batch_expanded["beat_plans"],
        "result": result,
    }


def regenerate_cinematic_coverage_plan(
    script_package: dict,
    narration_package: Optional[dict],
    visual_package: dict,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
    *,
    force: bool = False,
    fixture_verification: Optional[dict] = None,
    probe_first_batch: bool = False,
) -> dict:
    """
    Isolated, explicit, planning-only regeneration of one episode's
    Cinematic Coverage Plan checkpoint.

    Consumes script_package (authoritative beat text/IDs) and
    narration_package (measured per-beat durations), both validated via
    validate_narration_completeness() before anything else happens --
    including before the single LLM call this function may make. Uses
    visual_package only as read-only creative context (scene_id,
    visual_spec, presentation_plan, narrative_signals per beat) exactly as
    run_cinematic_director() does; never mutates it.

    force (required for ANY write):
      False (default) -- if the checkpoint already exists, returns it
        unchanged (status="cached", zero cost, identical to
        run_cinematic_director()'s own resume behavior). If no checkpoint
        exists, raises CoverageRegenerationError -- this entrypoint never
        generates a plan implicitly; regeneration always requires an
        explicit, deliberate force=True.
      True -- runs the full validate → backup → generate-to-candidate →
        validate-candidate → atomic-install sequence below.

    On force=True:
      1. Validate inputs (raises NarrationCompletenessError on failure,
         before any LLM call -- see requirement above).
      2. If a checkpoint already exists, copy it unchanged to a timestamped
         backup path (cinematic_coverage_plan_{episode_id}.backup_{ts}.json)
         before touching anything else.
      3. Make a deterministic, bounded sequence of strict batch LLM calls,
         non-streaming, one requests.post call maximum per batch (see the
         "Batched strict planning" module comment above
         _partition_beats_into_batches() for why: a single 47-beat/130-slot
         strict-schema request was rejected by the provider before
         generation began -- "The compiled grammar is too large" -- so the
         47 authoritative beats are partitioned deterministically, in
         authoritative order, into batches of at most
         _MAX_SLOTS_PER_BATCH shot slots each, never splitting one beat
         across two batches). Each batch requests strict structured output
         (response_format: json_schema, strict; provider.require_
         parameters=true; allow_model_fallback=False; max_attempts=1 — see
         the "Structured-output wire format" module comment above
         _structured_payload_to_compact()) at a deterministic, batch-sized
         max_tokens ceiling (_estimate_batch_max_tokens()), constraining
         generation itself to that batch's exact beat set, each beat's
         exact shot-slot count, and every enum position's authoritative
         vocabulary. Each batch's response is reshaped by
         _structured_payload_to_compact() into the same compact dict shape
         the prompt-only path used to produce directly, and accumulated
         purely in memory. A failure at any batch discards every batch
         already collected and rejects the whole attempt immediately — no
         later batch is submitted, nothing partial is ever installed, and a
         later invocation always restarts from batch 1. Only once every
         batch has succeeded are the merged beats expanded back to the
         exact full canonical shape by the unchanged _expand_compact_plan(),
         before any schema validation runs. Calls
         hypogeum.openrouter.call_claude() directly (not run_agent())
         specifically so each batch response is never checked against the
         full-schema validator until after reshaping and expansion.
         Nothing is written to disk until this function explicitly writes
         the expanded, merged candidate itself, at
         cinematic_coverage_plan_{episode_id}.candidate.json.
      4. Validate the candidate completely: every authoritative beat_id
         present exactly once, each beat_plan's shot count matching the
         shot_budget this function itself computed and sent as input
         (arithmetic mismatch otherwise), and -- only when the caller
         supplies fixture_verification -- the candidate's totals matching
         the caller-supplied expected values exactly (a caller-specific
         check; this function has no built-in expectations for any
         episode).
      5. Only on full success: atomically replace the active checkpoint
         with the candidate (Path.replace(), same filesystem, atomic) and
         return status="installed".
      6. On ANY failure at step 3-4 (LLM/network error, malformed output,
         schema-invalid output, arithmetic mismatch, fixture-verification
         mismatch): leave the active checkpoint exactly as it was, keep the
         backup from step 2, stop cleanly, and return status="rejected"
         with a `reason`. Never falls back to a minimal pass-through plan
         (unlike run_cinematic_director()) -- a rejected regeneration must
         never silently replace a working plan with a worse one.

    Never calls Shot Diversity Planner, Stage B, MUAPI, any other provider,
    Assembly, or Publishing, and never writes to the database or the
    asset_manifest -- this function returns immediately after step 5 or 6
    above in every case.

    probe_first_batch (2026-08-09, requires force=True; ignored otherwise):
      When True, this function computes the exact same deterministic batch
      partition described above, then submits ONLY batch 1 through
      _execute_single_batch() -- the identical shared helper ordinary
      batching itself calls, so a probe genuinely exercises the exact
      production request construction, never a parallel implementation of
      it -- and returns immediately with status="probe_succeeded" or
      status="probe_rejected". Batch 2 is never submitted, no full-plan
      merge happens, no candidate is ever written, the active checkpoint is
      never installed or otherwise mutated, and (since nothing is being
      installed, there is nothing to protect) no active-plan backup is
      created either -- step 2's backup-writing is skipped entirely for a
      probe. At most one requests.post call total is reachable. Intended
      as a cheap, safe, side-effect-free feasibility check before
      authorizing a full six-batch regeneration attempt.

    Returns a result dict:
      status:            "cached" | "installed" | "rejected" |
                          "probe_succeeded" | "probe_rejected"
      reason:            None, or why a "rejected"/"probe_rejected"
                          regeneration failed
      active_plan_path:  str path to the canonical checkpoint
      backup_path:       str path to the timestamped backup, or None
                          (always None for a probe -- see above)
      candidate_path:     str path to the candidate file, or None
      plan:              the plan now active on disk (unchanged if
                          "rejected"/"cached", the new one if "installed",
                          None for either probe outcome -- a probe never
                          produces a full canonical plan)
      totals:            {"main_shots", "short_shots", "total_shots",
                          "sufficient_beats", "insufficient_beats"} for
                          whichever plan `plan` refers to, or None if the
                          checkpoint didn't exist and force=True regenerated
                          from nothing, or either probe outcome (still
                          populated once installed)
      probe_batch_index, probe_batch_count, probe_batch_beat_ids:
                          only present for "probe_succeeded"/"probe_rejected"
                          -- which batch was probed, out of how many total,
                          and its exact beat_id list

    Raises:
      NarrationCompletenessError — see validate_narration_completeness().
      CoverageRegenerationError  — force=False with no existing checkpoint.
      ValueError                 — a single beat's own shot budget exceeds
                                    _MAX_SLOTS_PER_BATCH (see
                                    _partition_beats_into_batches()) --
                                    raised before any backup or provider
                                    call, exactly like
                                    NarrationCompletenessError.
    """
    import shutil
    from datetime import datetime, timezone

    from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR  # noqa: PLC0415
    from hypogeum.muapi import get_requested_clip_duration_seconds  # noqa: PLC0415
    from hypogeum.schemas import SchemaError, validate  # noqa: PLC0415

    _resolve_dir: Path = (
        Path(output_dir) if output_dir is not None
        else _DEFAULT_ASSETS_ROOT / "episodes" / episode_id
    )
    _resolve_dir.mkdir(parents=True, exist_ok=True)
    log = get_logger(episode_id=episode_id, agent="cinematic_director_regen")

    ckpt_path = _resolve_dir / f"cinematic_coverage_plan_{episode_id}.json"

    if not force:
        if ckpt_path.exists():
            log.info("coverage_regen_cached", episode_id=episode_id, path=str(ckpt_path))
            return {
                "status": "cached",
                "reason": None,
                "active_plan_path": str(ckpt_path),
                "backup_path": None,
                "candidate_path": None,
                "plan": json.loads(ckpt_path.read_text(encoding="utf-8")),
                "totals": None,
            }
        raise CoverageRegenerationError(
            f"no existing coverage plan checkpoint at {ckpt_path} and force=False -- "
            "this entrypoint never generates a plan implicitly; pass force=True to regenerate"
        )

    # Step 1 — validate inputs BEFORE the LLM call (and before any backup).
    measured_durations = validate_narration_completeness(script_package, narration_package)
    authoritative = list(measured_durations.keys())
    beat_texts_by_id = authoritative_beat_texts(script_package)
    beat_classification = _classify_authoritative_beats(script_package)

    provider_clip_duration = get_requested_clip_duration_seconds(config)
    coverage_capacity_per_shot = max(0.1, provider_clip_duration * MAX_EDITORIAL_SPEED_FACTOR)

    log.info(
        "coverage_regen_started",
        episode_id=episode_id,
        authoritative_beats=len(authoritative),
        force=True,
        probe_first_batch=probe_first_batch,
    )

    # Deterministic per-beat shot budgets, scene context, and the batch
    # partition itself -- computed before any backup or provider call, so
    # the first-batch feasibility probe below never needs step 2's backup
    # logic at all (a probe never installs, so there is nothing to protect).
    expected_shot_budget_by_beat_id: dict = {}
    scenes_by_beat_id: dict = {}
    for beat_id in authoritative:
        duration = measured_durations[beat_id]
        expected_shot_budget_by_beat_id[beat_id] = compute_shot_budget(duration, config)
        scenes_by_beat_id[beat_id] = next(
            (s for s in visual_package.get("scenes", []) if s.get("narration_reference") == beat_id),
            {},
        )

    batches = _partition_beats_into_batches(authoritative, expected_shot_budget_by_beat_id)

    if probe_first_batch:
        # First-batch feasibility probe: exactly one requests.post call
        # maximum, against batch 1 through _execute_single_batch() -- the
        # exact same helper ordinary batching itself calls below. Never
        # batch 2, never a full-plan merge, never a candidate write, never
        # installation or any active-plan mutation, never an active-plan
        # backup, never Stage B or any downstream work.
        probe_outcome = _execute_single_batch(
            1, len(batches), batches[0], expected_shot_budget_by_beat_id,
            measured_durations, scenes_by_beat_id, visual_package, episode_id,
            config, provider_clip_duration,
        )
        if probe_outcome["status"] == "batch_failed":
            log.error(
                "coverage_regen_probe_rejected", episode_id=episode_id,
                reason=probe_outcome["reason"],
            )
            return {
                "status": "probe_rejected",
                "reason": probe_outcome["reason"],
                "active_plan_path": str(ckpt_path),
                "backup_path": None,
                "candidate_path": None,
                "plan": None,
                "totals": None,
                "probe_batch_index": 1,
                "probe_batch_count": len(batches),
                "probe_batch_beat_ids": batches[0],
            }
        log.info(
            "coverage_regen_probe_succeeded", episode_id=episode_id,
            batch_beat_ids=batches[0], batch_count=len(batches),
        )
        return {
            "status": "probe_succeeded",
            "reason": None,
            "active_plan_path": str(ckpt_path),
            "backup_path": None,
            "candidate_path": None,
            "plan": None,
            "totals": None,
            "probe_batch_index": 1,
            "probe_batch_count": len(batches),
            "probe_batch_beat_ids": batches[0],
        }

    # Step 2 — preserve the existing plan as a timestamped backup, before
    # touching anything else.
    backup_path: Optional[Path] = None
    if ckpt_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = _resolve_dir / f"cinematic_coverage_plan_{episode_id}.backup_{ts}.json"
        shutil.copy2(ckpt_path, backup_path)
        log.info("coverage_regen_backup_written", episode_id=episode_id, path=str(backup_path))

    def _current_active_plan_and_totals() -> tuple:
        if ckpt_path.exists():
            active_plan = json.loads(ckpt_path.read_text(encoding="utf-8"))
            return active_plan, _compute_totals(
                active_plan, measured_durations, beat_classification, coverage_capacity_per_shot,
            )
        return None, None

    def _rejected(reason: str) -> dict:
        active_plan, totals = _current_active_plan_and_totals()
        log.error("coverage_regen_rejected", episode_id=episode_id, reason=reason)
        return {
            "status": "rejected",
            "reason": reason,
            "active_plan_path": str(ckpt_path),
            "backup_path": str(backup_path) if backup_path else None,
            "candidate_path": None,
            "plan": active_plan,
            "totals": totals,
        }

    # Step 3 — a deterministic, bounded sequence of strict batch requests
    # (see the "Batched strict planning" module comment above
    # _partition_beats_into_batches() for why this replaced a single
    # 47-beat/130-slot request -- that request was rejected by the provider
    # before generation began: "The compiled grammar is too large"). Each
    # batch runs through _execute_single_batch() -- the same shared helper
    # the probe above uses for batch 1 alone -- one independent
    # one-request-maximum call per batch; a failure at any batch discards
    # every batch already collected in memory and rejects the whole
    # attempt -- nothing is written to disk and no batch result is treated
    # as resumable state until every batch has succeeded and the full
    # merged candidate has passed every validator below. A later invocation
    # always starts again from batch 1.
    all_expanded_beat_plans: list = []
    for batch_index, batch_beat_ids in enumerate(batches, start=1):
        outcome = _execute_single_batch(
            batch_index, len(batches), batch_beat_ids, expected_shot_budget_by_beat_id,
            measured_durations, scenes_by_beat_id, visual_package, episode_id,
            config, provider_clip_duration,
        )
        if outcome["status"] == "batch_failed":
            return _rejected(outcome["reason"])

        # In-memory accumulation only -- no disk write, no partial-batch
        # artifact, until every batch above has succeeded.
        all_expanded_beat_plans.extend(outcome["beat_plans"])


    # All batches succeeded -- merge in memory (authoritative order is
    # preserved by construction: batches were built from `authoritative` in
    # order, and each batch's own expansion preserves its own sub-order)
    # and run the canonical schema validator once against the fully
    # assembled candidate, exactly as the single-request design did.
    merged_beat_ids = [bp["beat_id"] for bp in all_expanded_beat_plans]
    if merged_beat_ids != authoritative:
        return _rejected(
            "batch_merge_order_mismatch: merged beat order does not match "
            "authoritative order after all batches succeeded"
        )

    candidate = {"episode_id": episode_id, "beat_plans": all_expanded_beat_plans}
    try:
        validate("cinematic_coverage_plan", candidate)
    except SchemaError as exc:
        return _rejected(f"malformed_output: {type(exc).__name__}: {exc}")

    # Candidate written to its own named, inspectable path (never the
    # canonical checkpoint) -- this IS the "temporary path" of record.
    candidate_path = _resolve_dir / f"cinematic_coverage_plan_{episode_id}.candidate.json"
    candidate_path.write_text(json.dumps(candidate, indent=2, ensure_ascii=False), encoding="utf-8")

    # Step 4 — validate the candidate completely.
    candidate_beat_ids = [bp.get("beat_id") for bp in candidate.get("beat_plans", [])]
    if sorted(candidate_beat_ids) != sorted(authoritative) or len(set(candidate_beat_ids)) != len(candidate_beat_ids):
        candidate_path.unlink(missing_ok=True)
        return _rejected(
            "candidate does not have exactly one beat_plan per authoritative beat "
            f"(got {len(candidate_beat_ids)} for {len(authoritative)} authoritative beats)"
        )

    mismatches = []
    for bp in candidate.get("beat_plans", []):
        beat_id = bp.get("beat_id")
        actual = len(bp.get("shots", []))
        expected = expected_shot_budget_by_beat_id.get(beat_id)
        if actual != expected:
            mismatches.append((beat_id, expected, actual))
    if mismatches:
        candidate_path.unlink(missing_ok=True)
        return _rejected(f"arithmetic_mismatch: expected vs actual shot counts {mismatches}")

    totals = _compute_totals(candidate, measured_durations, beat_classification, coverage_capacity_per_shot)

    if fixture_verification is not None:
        fixture_mismatches = {
            key: (fixture_verification[key], totals[totals_key])
            for key, totals_key in (
                ("expected_main_shots", "main_shots"),
                ("expected_short_shots", "short_shots"),
                ("expected_total_shots", "total_shots"),
                ("expected_sufficient_beats", "sufficient_beats"),
            )
            if key in fixture_verification and fixture_verification[key] != totals[totals_key]
        }
        if fixture_mismatches:
            candidate_path.unlink(missing_ok=True)
            return _rejected(f"fixture_verification_mismatch: {fixture_mismatches}")

    # Step 5 — atomic install. Same directory/filesystem as ckpt_path, so
    # Path.replace() is an atomic rename, never a partially-written file.
    candidate_path.replace(ckpt_path)
    log.info(
        "coverage_regen_installed",
        episode_id=episode_id,
        path=str(ckpt_path),
        backup_path=str(backup_path) if backup_path else None,
        **totals,
    )

    return {
        "status": "installed",
        "reason": None,
        "active_plan_path": str(ckpt_path),
        "backup_path": str(backup_path) if backup_path else None,
        "candidate_path": str(ckpt_path),  # candidate_path was renamed onto ckpt_path
        "plan": candidate,
        "totals": totals,
    }


def _compute_totals(
    plan: dict,
    measured_durations: dict,
    beat_classification: dict,
    coverage_capacity_per_shot: float,
) -> dict:
    """Generic main/short/total shot counts and sufficient-beat count for
    `plan` -- no episode-specific numbers live here; the caller compares
    the result against its own expectations (see fixture_verification in
    regenerate_cinematic_coverage_plan())."""
    main_shots = 0
    short_shots = 0
    sufficient_beats = 0
    total_beats = 0
    for bp in plan.get("beat_plans", []):
        beat_id = bp.get("beat_id")
        n_shots = len(bp.get("shots", []))
        cls = beat_classification.get(beat_id)
        if cls == "main":
            main_shots += n_shots
        elif cls == "short":
            short_shots += n_shots
        total_beats += 1
        duration = measured_durations.get(beat_id, 0.0)
        if n_shots * coverage_capacity_per_shot >= duration - 1e-9:
            sufficient_beats += 1
    return {
        "main_shots": main_shots,
        "short_shots": short_shots,
        "total_shots": main_shots + short_shots,
        "sufficient_beats": sufficient_beats,
        "insufficient_beats": total_beats - sufficient_beats,
    }
