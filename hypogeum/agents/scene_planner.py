"""
hypogeum/agents/scene_planner.py — Scene planning utilities (Priority 7 extraction)

Extracted from visual_package_director.py. Responsible for:
  - Compacting script beats and packages for Claude input
  - Building the full scene-planning input package with generation constraints
  - Ordering and filtering planned scenes (by index, shorts suppression, cap)

Not responsible for:
  - Making Claude API calls (run_agent lives in VPD)
  - MUAPI generation
  - Entity continuity locks
  - Thumbnail handoff brief
  - Asset manifest assembly
  - Publishing

Public API:
    compact_beat(beat)
    compact_script_package(sp, *, strip_shorts=False)
    build_scene_plan_input(script_package, config, episode_id)
    order_and_filter_scenes(scenes, config, log)
"""

from __future__ import annotations

from typing import Optional

from hypogeum.config import Config


# ---------------------------------------------------------------------------
# Provider capability context injected into every Claude scene-planning call.
# Externalised here so it can be updated without changing the prompt file.
# ---------------------------------------------------------------------------

PROVIDER_CAPABILITIES: dict = {
    "muapi_video": {
        "supported_durations_seconds": [8],
        "duration_note": (
            "Only 8 is valid for production. Veo3 generates fixed 8s clips. "
            "Do not request 5s durations — use 8 for all scenes."
        ),
        "aspect_ratios": {
            "main_episode": "16:9",
            "shorts": "9:16",
        },
        "quality_tiers": {
            "1": "premium quality — veo3-text-to-video ($2.50/gen); reserve for hero/opening scenes",
            "2": "standard — veo3.1-lite-text-to-video ($0.39/gen, 1080p candidate) [primary production tier; use for all scenes unless T1 is justified]",
            "3": "cheap-mode alias — veo3.1-lite-text-to-video ($0.39/gen, 1080p candidate) [same model as T2; used when MUAPI_CHEAP_MODE=true]",
        },
        "i2v_note": (
            "When a final-locked entity has a reference image, Python code injects "
            "the hosted URL into submit_scene() automatically. You do not set "
            "reference_image_url in your output — plan entity_appearances[] only."
        ),
    },
}


# ---------------------------------------------------------------------------
# Beat / script package compaction
# ---------------------------------------------------------------------------

def compact_beat(beat: dict) -> dict:
    """Return a minimal beat dict with narration text capped at 20 words."""
    words = beat.get("text", "").split()
    short_text = " ".join(words[:20]) + ("…" if len(words) > 20 else "")
    return {
        "beat_id": beat.get("beat_id", ""),
        "beat_type": beat.get("beat_type", ""),
        "text": short_text,
    }


def compact_script_package(sp: dict, *, strip_shorts: bool = False) -> dict:
    """
    Return a compact version of script_package for VPD input.

    Preserves all structural fields (section/beat IDs, types, visual_directions)
    but truncates narration beat text to 20 words. This prevents the VPD input
    from exceeding ~8 KB and keeps VPD output within token budget.

    Args:
        sp:           The full script_package dict.
        strip_shorts: If True, omit shorts_scripts entirely (Episode Zero mode —
                      generation_constraints already communicates shorts_count=0).
    """
    compact: dict = {
        "episode_metadata": sp.get("episode_metadata", {}),
    }
    compact["sections"] = [
        {
            "section_id": s.get("section_id", ""),
            "title": s.get("title", ""),
            "visual_directions": s.get("visual_directions", ""),
            "narration_beats": [compact_beat(b) for b in s.get("narration_beats", [])],
        }
        for s in sp.get("sections", [])
    ]
    if not strip_shorts:
        compact["shorts_scripts"] = [
            {
                "short_id": sh.get("short_id", ""),
                "title": sh.get("title", ""),
                "visual_directions": sh.get("visual_directions", ""),
                "narration_beats": [compact_beat(b) for b in sh.get("narration_beats", [])],
            }
            for sh in sp.get("shorts_scripts", [])
        ]
    else:
        compact["shorts_scripts"] = []
    return compact


# ---------------------------------------------------------------------------
# Scene planning input construction
# ---------------------------------------------------------------------------

def build_scene_plan_input(
    script_package: dict,
    config: Config,
    episode_id: str,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Build the full input_package for the Claude scene-planning call.

    Determines episode zero / shorts-suppression mode and injects
    generation_constraints accordingly. Compacts the script_package
    before inclusion.

    Args:
        script_package: Full script_package dict from the Script Engine.
        config:         Loaded Config instance.
        episode_id:     Episode identifier used to detect episode zero mode.

    Returns:
        input_package dict ready to pass to run_agent().
    """
    short_native = (getattr(config, "SHORT_NATIVE_MODE", False) is True)
    is_episode_zero = (
        episode_id == "ep_000"
        or config.EPISODE_ZERO_MAX_SCENES > 0
    )
    dedicated_shorts_enabled = config.ENABLE_DEDICATED_SHORT_GENERATION
    suppress_shorts = short_native or is_episode_zero or not dedicated_shorts_enabled

    input_package: dict = {
        "task": "plan_visual_package",
        "script_package": compact_script_package(
            script_package, strip_shorts=suppress_shorts
        ),
        "provider_capabilities": PROVIDER_CAPABILITIES,
    }
    try:
        from hypogeum.channel_creative_brief import channel_brief_for
        input_package["channel_brief"] = channel_brief_for(channel_id)
    except Exception:  # noqa: BLE001
        pass

    if short_native:
        # One vertical deliverable: ONE 9:16 scene per narration beat, so
        # each beat is covered by its own single video clip and Assembly
        # never reaches the coverage-fill path. The script engine already
        # caps every beat's narration at the channel model's clip length.
        #
        _hard_cap = getattr(config, "SHORT_NATIVE_MAX_SCENES", 8) or 8
        _ez = config.EPISODE_ZERO_MAX_SCENES or 0
        if _ez and _ez < _hard_cap:
            # A genuine Episode-Zero validation cap (small) — honour it.
            _hard_cap = _ez
        elif _ez and _ez > _hard_cap:
            # apply_short_native_preset() scaled the cap UP for a shorter-clip
            # model (5s clips need more scenes than the 8-scene default to
            # hold a full story arc — otherwise the ending beats get no video
            # and the short is visibly cut off). Take the scaled value.
            _hard_cap = _ez
        _beat_count = sum(
            len(sec.get("narration_beats", []) or [])
            for sec in script_package.get("sections", []) or []
        )
        _scene_cap = min(_beat_count, _hard_cap) if _beat_count else _hard_cap
        input_package["generation_constraints"] = {
            "max_main_scenes": _scene_cap,
            "scene_granularity": "exactly one scene per narration beat",
            "shorts_count": 0,
            "mode": "short_native",
            "aspect_ratio": getattr(config, "SHORT_NATIVE_ASPECT_RATIO", "9:16"),
        }
    elif is_episode_zero:
        input_package["generation_constraints"] = {
            "max_main_scenes": config.EPISODE_ZERO_MAX_SCENES or 5,
            "shorts_count": 0,
            "mode": "validation",
        }
    elif not dedicated_shorts_enabled:
        input_package["generation_constraints"] = {
            "shorts_count": 0,
        }

    return input_package


# ---------------------------------------------------------------------------
# Scene ordering and filtering
# ---------------------------------------------------------------------------

def order_and_filter_scenes(
    scenes: list,
    config: Config,
    log,
) -> list:
    """
    Sort scenes by scene_index, filter short scenes if disabled, apply cap.

    Filtering is applied before the cap so the cap counts only deliverable scenes.

    Args:
        scenes: List of planned scene dicts from the visual_package.
        config: Loaded Config instance.
        log:    Logger instance for warning events.

    Returns:
        Ordered and filtered list of scene dicts. Input list is not mutated.
    """
    scenes_sorted = sorted(scenes, key=lambda s: s.get("scene_index", 0))

    # ── Short-native: force the deliverable aspect ratio deterministically.
    # generation_constraints.aspect_ratio is a *soft* hint the LLM has been
    # observed to ignore, leaving production_spec.aspect_ratio at the "16:9"
    # default → veo generates landscape → production_output_gate rejects every
    # clip (it requires 1080x1920 for a short-native episode). This pass is the
    # single deterministic guarantee that every scene is portrait before it
    # reaches muapi.submit_scene (which reads visual_spec.aspect_ratio, sourced
    # from production_spec.aspect_ratio in the VPD).
    if getattr(config, "SHORT_NATIVE_MODE", False) is True:
        want = getattr(config, "SHORT_NATIVE_ASPECT_RATIO", "9:16") or "9:16"
        forced = 0
        _out = []
        for s in scenes_sorted:
            ps = dict(s.get("production_spec") or {})
            if ps.get("aspect_ratio") != want:
                forced += 1
            ps["aspect_ratio"] = want
            s = {**s, "production_spec": ps}
            _out.append(s)
        scenes_sorted = _out
        if forced:
            log.warn(
                "short_native_aspect_forced",
                scenes=forced,
                total=len(scenes_sorted),
                aspect_ratio=want,
                reason="LLM left production_spec.aspect_ratio non-portrait",
            )

    # Defense-in-depth: if dedicated short generation is disabled, strip any short
    # scenes that Claude may have planned despite generation_constraints.shorts_count=0.
    if not config.ENABLE_DEDICATED_SHORT_GENERATION:
        short_scenes = [s for s in scenes_sorted if s.get("source_type") == "short"]
        if short_scenes:
            blocked_ids = [s.get("scene_id") for s in short_scenes]
            log.warn(
                "vpd_short_scenes_blocked",
                count=len(blocked_ids),
                scene_ids=blocked_ids,
                reason="ENABLE_DEDICATED_SHORT_GENERATION=false",
            )
        scenes_sorted = [s for s in scenes_sorted if s.get("source_type") != "short"]

    # M5.6 — Episode Zero scene cap
    max_scenes = config.EPISODE_ZERO_MAX_SCENES
    if max_scenes > 0 and len(scenes_sorted) > max_scenes:
        log.warn(
            "vpd_scene_cap_applied",
            total_planned=len(scenes_sorted),
            cap=max_scenes,
            skipped=len(scenes_sorted) - max_scenes,
        )
        scenes_sorted = scenes_sorted[:max_scenes]

    return scenes_sorted
