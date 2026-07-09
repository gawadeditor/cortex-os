"""
hypogeum/agents/shot_diversity_planner.py — Shot Diversity Planner (V3)

Ensures visual variety across an episode by assigning shot_scale, camera_movement,
camera_angle, composition, color_palette, environment, subject, visual_motif, and
reuse_allowed to every scene before cinematic prompts are written (Stage B).

Runs AFTER Stage A4 (presentation planning) and BEFORE Stage B (prompt writing),
so Stage B receives both presentation_plan and diversity_assignment as constraints.

Public API:
    run_shot_diversity_planner(
        visual_package, episode_id, config, output_dir
    ) → dict  (shot_diversity_plan, schema-validated, written to disk)

Checkpoint: sdp_{episode_id}.json

Logging events:
    shot_diversity_planner_started     — info  — before run_agent() call
    shot_diversity_planner_resumed     — info  — checkpoint found, skipping call
    shot_diversity_planner_completed   — info  — plan written to checkpoint
    shot_diversity_violations_found    — warn  — violations list is non-empty
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.prompt_loader import compose_system_prompt
from hypogeum.schemas import validate
from hypogeum import skill_registry as _skill_registry

_AGENT_NAME = "shot_diversity_planner"
_CLAUDE_SONNET = "anthropic/claude-sonnet-4.6"
_MAX_TOKENS = 4_096

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "shot_diversity_planner.txt"

# Visual Language Engine — provides shot scale, camera movement, and
# composition vocabulary used when assigning diversity attributes to scenes.
# B1: compact runtime version used by default (~5.1K chars vs 11K full).
# Full skill retained at hypogeum/skills/visual_intelligence/visual_language_engine.md.
_COMPILED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "compiled"
_VLE_SKILL_FILE = _COMPILED_SKILLS_DIR / "visual_language_engine.compact.md"

_SDP_ALL_SKILL_PATHS: list[Path] = [_VLE_SKILL_FILE]

_DEFAULT_ASSETS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / ".hypogeum-assets"
)


def _load_prompt() -> str:
    return compose_system_prompt(_PROMPT_FILE, _VLE_SKILL_FILE)


SYSTEM_PROMPT: str = _load_prompt()


def run_shot_diversity_planner(
    visual_package: dict,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Assign visual diversity attributes to every scene in the visual_package.

    Args:
        visual_package: Visual Package dict (after Stage A4 has injected presentation_plan
                        into each scene).
        episode_id:     Episode identifier for logging and file naming.
        config:         Loaded Config instance.
        output_dir:     Optional output directory override.

    Returns:
        shot_diversity_plan dict (schema-validated, written to disk).
        On error, returns a minimal valid plan with an empty scene_assignments list
        so the pipeline can continue without diversity enforcement.
    """
    _resolve_dir: Path = (
        Path(output_dir) if output_dir is not None
        else _DEFAULT_ASSETS_ROOT / "episodes" / episode_id
    )
    _resolve_dir.mkdir(parents=True, exist_ok=True)

    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)
    _skill_registry.log_loaded_skills(_AGENT_NAME, _SDP_ALL_SKILL_PATHS, log)

    ckpt_path = _resolve_dir / f"sdp_{episode_id}.json"

    if ckpt_path.exists():
        log.info(
            "shot_diversity_planner_resumed",
            path=str(ckpt_path),
            episode_id=episode_id,
        )
        return json.loads(ckpt_path.read_text(encoding="utf-8"))

    scenes = visual_package.get("scenes", [])
    episode_metadata = visual_package.get("episode_metadata", {})

    scenes_for_planner = [
        {
            "scene_id": s.get("scene_id"),
            "scene_index": s.get("scene_index"),
            "source_type": s.get("source_type"),
            "visual_spec": s.get("visual_spec", {}),
            "presentation_plan": s.get("presentation_plan", {}),
        }
        for s in scenes
    ]

    planner_input = {
        "task": "plan_shot_diversity",
        "episode_metadata": {
            "episode_title": episode_metadata.get("episode_title", ""),
            "total_scenes": len(scenes_for_planner),
            "episode_id": episode_id,
        },
        "scenes": scenes_for_planner,
    }

    log.info(
        "shot_diversity_planner_started",
        episode_id=episode_id,
        scenes=len(scenes_for_planner),
        max_tokens=_MAX_TOKENS,
    )

    try:
        result = run_agent(
            agent_name=_AGENT_NAME,
            model_slug=_CLAUDE_SONNET,
            system_prompt=SYSTEM_PROMPT,
            input_package=planner_input,
            output_schema="shot_diversity_plan",
            episode_id=episode_id,
            max_tokens=_MAX_TOKENS,
            config=config,
            output_dir=None,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "shot_diversity_planner_failed",
            episode_id=episode_id,
            error=str(exc),
            note="Returning minimal pass-through plan; diversity not enforced",
        )
        # Return a minimal valid plan so pipeline can continue
        result = _minimal_pass_through(episode_id, scenes)

    violations = result.get("violations", [])
    if violations:
        log.warn(
            "shot_diversity_violations_found",
            episode_id=episode_id,
            violation_count=len(violations),
            violations=[v.get("violation_type") for v in violations],
        )

    ckpt_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info(
        "shot_diversity_planner_completed",
        episode_id=episode_id,
        assignments=len(result.get("scene_assignments", [])),
        diversity_score=result.get("diversity_score", 0.0),
        path=str(ckpt_path),
    )

    return result


def _minimal_pass_through(episode_id: str, scenes: list) -> dict:
    """
    Return a minimal shot_diversity_plan that passes schema validation.

    Used when the LLM call fails so the pipeline can continue without
    diversity enforcement. Each scene gets generic attributes.
    """
    _scales = [
        "wide_establishing", "medium", "close_up",
        "medium_wide", "extreme_close_up",
    ]
    _movements = [
        "slow_push_in", "controlled_pull_back", "lateral_dolly_left",
        "lateral_dolly_right", "vertical_descent",
    ]

    assignments = []
    for i, scene in enumerate(scenes):
        assignments.append({
            "scene_id": scene.get("scene_id", f"s{i:02d}"),
            "shot_scale": _scales[i % len(_scales)],
            "camera_movement": _movements[i % len(_movements)],
            "camera_angle": "eye_level",
            "composition": "rule_of_thirds_left",
            "color_palette": "warm amber earth tones",
            "environment": scene.get("visual_spec", {}).get("setting", "historical environment"),
            "subject": scene.get("visual_spec", {}).get("subject", "historical subject"),
            "visual_motif": "ancient stone light",
            "reuse_allowed": True,
        })

    unique_scales = list({a["shot_scale"] for a in assignments})
    unique_movements = list({a["camera_movement"] for a in assignments})
    unique_envs = list({a["environment"] for a in assignments})
    unique_comps = list({a["composition"] for a in assignments})

    n = len(assignments)
    diversity_score = (
        len({(a["shot_scale"], a["camera_movement"]) for a in assignments}) / n
        if n > 0 else 0.0
    )

    return {
        "episode_id": episode_id,
        "diversity_map": {
            "shot_scales": unique_scales,
            "camera_movements": unique_movements,
            "environments": unique_envs,
            "compositions": unique_comps,
        },
        "scene_assignments": assignments,
        "violations": [],
        "diversity_score": round(diversity_score, 3),
    }
