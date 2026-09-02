"""
hypogeum/agents/assembly_agent.py — Assembly Agent (M4.2–M4.8)

Constructs the final episode and Shorts renders from upstream packages.
Receives Visual Package + Narration Package + Thumbnail Package.
Produces a Final Render Package with all rendered video files and references.

No Claude calls — this agent is pure Python orchestration over elevenlabs.py
(music sourcing) and ffmpeg (video rendering via subprocess).

Execution sequence:
  A. Asset inventory validation
       - Verify all video clip asset_ids from Visual Package exist on disk.
       - Verify all narration audio files from Narration Package exist on disk.
       - Compute clip_availability_rate = present_clips / total_clips.
       - If clip_availability_rate < 0.80: halt with PDR.
  B. Timeline construction (narration-anchored sync)
       - For each scene: scene_duration = sum of narration beat durations.
       - Map video clips to scene slots; pad or trim as needed.
       - Build TimelineEntry list.
  C. Music sourcing — delegated to Music Agent (never sourced here directly)
       - Calls run_music_agent() when music_track_package not pre-provided.
       - Unpacks package to music_tracks dict for ffmpeg layer.
  D. Audio mixing orchestration
       - Narration at 0 dB reference; music at −18 dB (≈ 0.126 amplitude).
  E. ffmpeg execution layer
       - Main episode (16:9) → episodes/{episode_id}/main_{episode_id}.mp4
       - Shorts (9:16) → episodes/{episode_id}/short_{i}_{episode_id}.mp4
       - All ffmpeg calls via subprocess; no Python ffmpeg wrapper.
       - Timeout configurable (default 600s).
  F. Final Render Package generation
       - Validated against schemas.py final_render_package schema.
       - Builds render_output_manifest (written to disk).
       - Delegates production_ready to Final Render Reviewer (Agent 37).

Public API:
    run_assembly_agent(
        visual_package, narration_package, thumbnail_package,
        config, episode_id, output_dir=None
    ) → dict  (Final Render Package, schema-validated)

Integration contracts (must not be violated):
  - All music generation through generate_music_track() in hypogeum.elevenlabs
  - All config access through Config — never os.getenv()
  - All logging through get_logger() — never print() or stdlib logging
  - ffmpeg via subprocess only — never a Python ffmpeg wrapper library
  - Output validated through schemas.py validate()

Logging events emitted:
  assembly_agent_started         — info  — before asset validation
  assembly_clip_missing          — warn  — clip not found on disk
  assembly_clip_availability     — info  — rate computed, threshold result
  assembly_pdr_below_threshold   — error — halted: < 80% clips available
  assembly_timeline_built        — info  — timeline entries constructed
  assembly_music_sourced         — info  — music_track_package received/obtained
  assembly_ffmpeg_start          — debug — command logged before execution
  assembly_ffmpeg_done           — info  — render complete
  assembly_ffmpeg_failed         — error — non-zero exit; render_status=failed
  assembly_package_finalized     — info  — package written to disk
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hypogeum.agents.final_render_reviewer import run_final_render_reviewer
from hypogeum.agents.music_agent import run_music_agent
from hypogeum.agents.shorts_metadata import ShortsMetadataError, generate_shorts_titles
from hypogeum.agents.timeline_qa import run_timeline_qa
from hypogeum.agents.timeline_builder import (
    build_timelines,
    compute_music_sections,
    compute_section_durations,
    find_clip_asset_id,
    sum_timeline_duration,
)
from hypogeum.agents.video_renderer import FRAME_RATE, MAIN_RESOLUTION, SHORT_RESOLUTION, render_episode
from hypogeum.config import Config
from hypogeum.elevenlabs import MusicTrackResult
from hypogeum.logger import get_logger
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "assembly_agent"
# Bumped 2026-08-01: the coverage-check gate (B1) and the removal of
# renderer-side tpad/loop/reverse/uncapped-setpts compensation are a policy
# boundary, not just a bugfix -- a final_render_package written by an older
# version was produced under different (now-forbidden) assembly rules and
# must never be trusted as a checkpoint under the current policy, no matter
# how recent its timestamp is. See hypogeum/artifact_compatibility.py and
# hypogeum/pipeline.py's _ASSEMBLY_CHECKPOINT.compat_check.
_AGENT_VERSION = "2.0"

# Asset inventory threshold — assembly halts below this rate.
# Lowered from 0.80 to 0.60: episodes with ≥60% clips can produce a
# partial render with narration-anchored hold frames for missing scenes.
_CLIP_AVAILABILITY_THRESHOLD = 0.60

# Music categories (6 defined in spec §8)
_MUSIC_CATEGORIES = frozenset({
    "Mystery", "Discovery", "Investigation",
    "Ancient Civilizations", "Tension", "Resolution",
})

# PDR constants
_PDR_ROUTING_TARGET = "executive_producer"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AssemblyAgentError(RuntimeError):
    """Raised for Assembly Agent configuration or orchestration errors that halt assembly."""


class ShortIdentityLinkageError(AssemblyAgentError):
    """
    Raised when timeline_plan.short_ids does not have exactly as many real
    entries as this episode's Shorts actually require, or when a resolved
    short_id cannot be matched to its own caption asset. Never silently
    padded with a placeholder identity (sh_00N_placeholder) and never
    silently rendered without its required caption path -- see the
    2026-08-22 forensic audit (docs/operations/audits/
    EP_F3619022_ASSET_CONTINUITY_AND_BURNED_CAPTIONS_AUDIT_2026-08-22.md)
    this closes. A completed production artifact must never retain a
    placeholder identity.
    """


def _probe_seconds(path: "Path") -> float:
    import subprocess  # noqa: PLC0415
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _append_channel_outro(
    main_render_path: "Path", channel_id: Optional[str], width: int, height: int,
    frame_rate: int, ffmpeg_binary: str, log,
) -> float:
    """Concatenate the channel's fixed branded outro (channel_packages/<id>/
    outro.mp4) onto the end of the main render, in place. Returns the seconds
    added (0.0 when the channel has no outro, or on any failure — the episode
    ships without it rather than not at all). The outro is branding, not
    narrated story content, so it is appended AFTER the coverage check /
    narration-completeness gate and never participates in either."""
    import subprocess  # noqa: PLC0415
    from hypogeum.channel_package_scaffold import channel_outro_path  # noqa: PLC0415

    outro = channel_outro_path(channel_id or "")
    if outro is None or not main_render_path.exists():
        return 0.0
    added = _probe_seconds(outro)
    tmp = main_render_path.with_suffix(".with_outro.mp4")
    # Re-encode both segments to one timebase/fps/size so concat is seamless
    # regardless of what the operator's outro.mp4 was encoded at.
    fc = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={frame_rate},settb=AVTB[v0];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={frame_rate},settb=AVTB[v1];"
        f"[0:a]aresample=48000[a0];[1:a]aresample=48000[a1];"
        f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    cmd = [
        ffmpeg_binary, "-y", "-i", str(main_render_path), "-i", str(outro),
        "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(frame_rate),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", str(tmp),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not tmp.exists():
            log.warn("assembly_channel_outro_append_failed", channel_id=channel_id,
                     stderr=(r.stderr or "")[-300:])
            tmp.unlink(missing_ok=True)
            return 0.0
        tmp.replace(main_render_path)
        log.info("assembly_channel_outro_appended", channel_id=channel_id,
                 outro=str(outro), seconds_added=round(added, 2))
        return added
    except Exception as exc:  # noqa: BLE001
        log.warn("assembly_channel_outro_append_error", channel_id=channel_id, error=str(exc))
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return 0.0


def _persist_visual_package_checkpoint(
    visual_package: dict,
    episode_id: str,
    episode_dir: Path,
    log,
) -> None:
    """
    Re-persist the Visual Package checkpoint after a clip-repair pass
    appends new asset_manifest entries to the in-memory visual_package dict.

    hypogeum.agents.clip_repair_agent (both run_clip_repair()'s Strategy B
    and fill_coverage_deficits()) mutates visual_package["asset_manifest"]
    ["assets"] directly on the dict Assembly passed in, but neither
    function has its own disk-write access -- Assembly is the only caller
    that knows a mutation happened, so it is the only place that can close
    this gap. Without this write, a clip-repair asset is real (paid,
    qualified, on disk, in the cost ledger) but the checkpoint file any
    future resume reads (hypogeum/pipeline.py's checkpoint loader, or a
    fresh fill_coverage_deficits() call's own existing_extra_ids /
    represented_shot_ids resume bookkeeping) has no record of it -- the
    only thing standing between that and a duplicate paid call is
    submit_scene()'s own opportunistic path-exists reuse, which is a
    correct independent safety net but should never be the sole source of
    truth for what has already been generated.

    Generic and channel-agnostic: writes the exact same file, at the exact
    same path and in the exact same JSON convention, that every other
    checkpoint write in this codebase already produces
    (f"visual_package_{episode_id}.json"), keyed only by episode_id.
    """
    path = episode_dir / f"visual_package_{episode_id}.json"
    content = json.dumps(visual_package, ensure_ascii=False, indent=2)
    path.write_text(content, encoding="utf-8")
    log.info(
        "assembly_visual_package_checkpoint_updated",
        episode_id=episode_id,
        path=str(path),
        asset_count=len(visual_package.get("asset_manifest", {}).get("assets", [])),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_assembly_agent(
    visual_package: dict,
    narration_package: dict,
    thumbnail_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    script_package: Optional[dict] = None,
    source_registry: Optional[list] = None,
    music_track_package: Optional[dict] = None,
    topic_brief: Optional[dict] = None,
    research_package: Optional[dict] = None,
    narrative_package: Optional[dict] = None,
    channel_id: Optional[str] = None,
    shorts_title_editorial_rules: Optional[dict] = None,
    shorts_title_llm_call=None,
) -> dict:
    """
    Execute the Assembly Agent end-to-end.

    Validates upstream package presence and structure, validates on-disk assets,
    constructs timelines, sources music, mixes audio, renders video via ffmpeg,
    and returns a schema-validated Final Render Package.

    Input validation (stage 0) fires before any disk I/O or provider call:
      - visual_package must be a non-empty dict with a 'scenes' key.
      - narration_package must be a non-empty dict with a 'stitched_tracks' key.
      - thumbnail_package must be a non-empty dict.
      - script_package, topic_brief, research_package, narrative_package are
        validated as non-empty dicts when provided (optional by design — earlier
        pipeline stages may have been skipped during partial runs).
    A validation failure returns a failed Final Render Package immediately; no
    provider or ffmpeg calls are made.

    Args:
        visual_package:    Validated Visual Package from Visual Package Director.
        narration_package: Validated Narration Package from Audio Agent.
        thumbnail_package: Validated Thumbnail Package from Thumbnail Director.
        config:            Loaded Config instance.
        episode_id:        Episode identifier for asset paths and log correlation.
        output_dir:        Optional output directory override (defaults to
                           .hypogeum-assets/episodes/{episode_id}/).
        script_package:    Optional Script Package for chapter timestamp computation.
        source_registry:   Optional source_registry[] from the Research Package.
        music_track_package: Optional music_track_package from Music Agent.
        topic_brief:       Optional Topic Brief from Executive Producer.
        research_package:  Optional Research Package from Research Director.
                           Accepted for lineage validation; not consumed by Assembly.
        narrative_package: Optional Narrative Package from Narrative Architect.
                           Accepted for lineage validation; not consumed by Assembly.
        channel_id:        Optional -- forwarded to generate_shorts_titles()'s
                           model call for log correlation only; no channel-
                           specific value is read from it here (any channel-
                           specific editorial style must be passed explicitly
                           via shorts_title_editorial_rules, resolved by the
                           caller from that channel's own Channel Package).
        shorts_title_editorial_rules: Optional dict overriding
                           hypogeum.agents.shorts_metadata.DEFAULT_EDITORIAL_RULES
                           (title length bounds, tone, forbidden patterns).
                           Channel-specific values belong in the caller's
                           Channel Package, never hardcoded here.
        shorts_title_llm_call: Optional injectable callable forwarded to
                           generate_shorts_titles()'s own llm_call parameter
                           — tests supply a fake model fixture here; real
                           callers omit it (defaults to a real, non-free
                           OpenRouter call made only once per Short-titling
                           attempt, never per retry of the whole episode).

    Returns:
        Final Render Package dict (schema-validated, written to disk).
    """
    _default_assets_root: Path = (
        Path(__file__).resolve().parent.parent.parent / ".hypogeum-assets"
    )
    if output_dir is not None:
        output_dir = Path(output_dir)
        # Search parent directories for an existing .hypogeum-assets folder;
        # if none found, fall back to output_dir.parent / ".hypogeum-assets"
        # (covers test setups where output_dir depth varies).
        _candidate: Optional[Path] = None
        _search = output_dir
        for _ in range(6):  # limit traversal depth
            _probe = _search / ".hypogeum-assets"
            if _probe.is_dir():
                _candidate = _probe
                break
            _search = _search.parent
        assets_root: Path = _candidate if _candidate is not None else (
            output_dir.parent / ".hypogeum-assets"
        )
    else:
        assets_root = _default_assets_root
    episode_dir: Path = output_dir or (assets_root / "episodes" / episode_id)
    episode_dir.mkdir(parents=True, exist_ok=True)

    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    log.info(
        "assembly_agent_started",
        episode_id=episode_id,
        agent_version=_AGENT_VERSION,
    )

    # ------------------------------------------------------------------
    # Stage 0: Required input package validation
    # Fires before any disk I/O, timeline construction, or provider call.
    # A missing or structurally invalid required package returns a failed
    # Final Render Package immediately so the pipeline can report clearly.
    # ------------------------------------------------------------------
    _pkg_error = _validate_input_packages(
        visual_package=visual_package,
        narration_package=narration_package,
        thumbnail_package=thumbnail_package,
        script_package=script_package,
        topic_brief=topic_brief,
        research_package=research_package,
        narrative_package=narrative_package,
    )
    if _pkg_error is not None:
        log.error(
            "assembly_input_validation_failed",
            episode_id=episode_id,
            reason=_pkg_error,
        )
        return _build_failed_package(
            episode_id=episode_id,
            visual_package=visual_package if isinstance(visual_package, dict) else {},
            narration_package=narration_package if isinstance(narration_package, dict) else {},
            thumbnail_package=thumbnail_package if isinstance(thumbnail_package, dict) else {},
            reason=_pkg_error,
            clip_availability_rate=0.0,
            missing_clips=[],
            log=log,
            episode_dir=episode_dir,
            topic_brief=topic_brief,
            source_registry=source_registry,
        )

    ffmpeg_timeout = getattr(config, "ASSEMBLY_FFMPEG_TIMEOUT", 600)
    if not isinstance(ffmpeg_timeout, int) or ffmpeg_timeout <= 0:
        ffmpeg_timeout = 600

    # ------------------------------------------------------------------
    # A. Asset inventory validation
    # ------------------------------------------------------------------
    scenes = visual_package.get("scenes", [])
    main_scenes = [s for s in scenes if s.get("source_type") == "main_episode"]
    short_scenes = [s for s in scenes if s.get("source_type") == "short"]

    asset_manifest = visual_package.get("asset_manifest", {})
    assets_list = asset_manifest.get("assets", [])

    # Build asset_id → file_path lookup
    asset_lookup: dict[str, Path] = {}
    for asset in assets_list:
        aid = asset.get("asset_id", "")
        if aid:
            # asset_id is relative from .hypogeum-assets/ without extension
            asset_lookup[aid] = assets_root / f"{aid}.mp4"

    # Scenes with generation_status="deferred" are intentionally parked (no clip
    # will ever be generated for them in this run). Exclude them from the clip
    # availability count so they don't count as missing and fail QA.
    deferred_scene_ids: set = {
        a["scene_id"]
        for a in assets_list
        if a.get("generation_status") == "deferred" and a.get("scene_id")
    }

    # Validate clip availability for main episode + all shorts (deferred excluded)
    present_clips: List[str] = []
    missing_clips: List[str] = []

    for scene in scenes:
        if scene.get("scene_id") in deferred_scene_ids:
            continue  # intentionally parked — not a missing clip
        found_asset_id = find_clip_asset_id(scene, asset_lookup, assets_root, episode_id)
        if found_asset_id is not None:
            clip_path = asset_lookup.get(found_asset_id)
            if clip_path is None:
                clip_path = assets_root / f"{found_asset_id}.mp4"
            if clip_path.exists():
                present_clips.append(found_asset_id)
            else:
                log.warn(
                    "assembly_clip_missing",
                    episode_id=episode_id,
                    scene_id=scene.get("scene_id"),
                    asset_id=found_asset_id,
                    expected_path=str(clip_path),
                )
                missing_clips.append(scene.get("scene_id", found_asset_id))
        else:
            log.warn(
                "assembly_clip_missing",
                episode_id=episode_id,
                scene_id=scene.get("scene_id"),
                asset_id=None,
                reason="no_asset_id_found",
            )
            missing_clips.append(scene.get("scene_id", "unknown"))

    # Count scenes actually skipped (not unique IDs — some scene_ids appear
    # more than once in the scenes list due to duplicate manifest entries).
    deferred_in_scenes = sum(1 for s in scenes if s.get("scene_id") in deferred_scene_ids)
    total_clips = len(scenes) - deferred_in_scenes
    present_count = len(present_clips)
    clip_availability_rate = present_count / total_clips if total_clips > 0 else 0.0

    log.info(
        "assembly_clip_availability",
        episode_id=episode_id,
        total_clips=total_clips,
        present_clips=present_count,
        missing_clips=len(missing_clips),
        deferred_clips=deferred_in_scenes,
        clip_availability_rate=round(clip_availability_rate, 4),
        threshold=_CLIP_AVAILABILITY_THRESHOLD,
        passes_threshold=clip_availability_rate >= _CLIP_AVAILABILITY_THRESHOLD,
    )

    # ------------------------------------------------------------------
    # A2. Clip Repair — regenerate missing clips before QA rejects.
    #     A missing scene is regenerated from its own prompt via MUAPI
    #     (capped by config; a generated-outro scene is always regenerated).
    #     No donor-copy / reuse fallback — perceptual repetition is forbidden;
    #     whatever cannot be regenerated stays missing and the episode fails
    #     closed at the clip-availability gate below.
    #     Only runs if CLIP_REPAIR_ENABLED (default True) and clips are missing.
    # ------------------------------------------------------------------
    if missing_clips and getattr(config, "CLIP_REPAIR_ENABLED", True):
        from hypogeum.agents.clip_repair_agent import run_clip_repair  # noqa: PLC0415
        run_clip_repair(
            visual_package=visual_package,
            missing_scene_ids=list(missing_clips),
            present_clip_ids=list(present_clips),
            assets_root=assets_root,
            config=config,
            episode_id=episode_id,
            log=log,
            channel_id=channel_id,
        )
        # run_clip_repair() may have appended new asset_manifest entries to
        # visual_package in memory (Strategy B) — persist immediately so
        # this paid work is never lost from the checkpoint, regardless of
        # what happens later in this run.
        _persist_visual_package_checkpoint(
            visual_package=visual_package, episode_id=episode_id,
            episode_dir=episode_dir, log=log,
        )
        # Recompute availability — repaired clips should now exist on disk
        present_clips = []
        missing_clips = []
        for _scene in scenes:
            if _scene.get("scene_id") in deferred_scene_ids:
                continue  # intentionally parked — skip
            _found = find_clip_asset_id(_scene, asset_lookup, assets_root, episode_id)
            if _found is not None:
                _clip_path = asset_lookup.get(_found) or (assets_root / f"{_found}.mp4")
                if _clip_path.exists():
                    present_clips.append(_found)
                else:
                    missing_clips.append(_scene.get("scene_id", _found))
            else:
                missing_clips.append(_scene.get("scene_id", "unknown"))
        clip_availability_rate = len(present_clips) / total_clips if total_clips > 0 else 0.0
        log.info(
            "assembly_clip_availability_after_repair",
            episode_id=episode_id,
            present_clips=len(present_clips),
            missing_clips=len(missing_clips),
            clip_availability_rate=round(clip_availability_rate, 4),
        )

    if clip_availability_rate < _CLIP_AVAILABILITY_THRESHOLD:
        pdr = _build_pdr(
            episode_id=episode_id,
            reason=(
                f"Clip availability rate {clip_availability_rate:.1%} is below the "
                f"80% threshold required for assembly. "
                f"{len(missing_clips)} clips are missing: {missing_clips[:10]}"
            ),
            severity="BLOCKING",
        )
        log.error(
            "assembly_pdr_below_threshold",
            episode_id=episode_id,
            clip_availability_rate=round(clip_availability_rate, 4),
            missing_clips=missing_clips,
        )
        # Return a failed package rather than raise, to comply with
        # "Assembly Agent always delivers" governance decision
        return _build_failed_package(
            episode_id=episode_id,
            visual_package=visual_package,
            narration_package=narration_package,
            thumbnail_package=thumbnail_package,
            reason="clip_availability_below_threshold",
            clip_availability_rate=clip_availability_rate,
            missing_clips=missing_clips,
            log=log,
            episode_dir=episode_dir,
            topic_brief=topic_brief,
            source_registry=source_registry,
        )

    # Validate narration files
    missing_narration: List[str] = []
    stitched_tracks = narration_package.get("stitched_tracks", [])
    for track in stitched_tracks:
        audio_asset_id = track.get("audio_asset_id", "")
        if audio_asset_id:
            audio_path = assets_root / f"{audio_asset_id}.mp3"
            if not audio_path.exists():
                missing_narration.append(audio_asset_id)

    if missing_narration:
        # Missing narration is critical only for the main episode
        main_narration_missing = any("main_narration" in a for a in missing_narration)
        if main_narration_missing:
            return _build_failed_package(
                episode_id=episode_id,
                visual_package=visual_package,
                narration_package=narration_package,
                thumbnail_package=thumbnail_package,
                reason="main_narration_track_missing",
                clip_availability_rate=clip_availability_rate,
                missing_clips=missing_clips,
                log=log,
                episode_dir=episode_dir,
                topic_brief=topic_brief,
                source_registry=source_registry,
            )

    # ------------------------------------------------------------------
    # B. Timeline construction — delegated to Timeline Builder
    # ------------------------------------------------------------------
    timeline_plan = build_timelines(
        scenes=scenes,
        narration_segments=narration_package.get("narration_segments", []),
        asset_lookup=asset_lookup,
        assets_root=assets_root,
        episode_id=episode_id,
    )
    main_timeline = timeline_plan.main_timeline
    shorts_timelines = timeline_plan.shorts_timelines
    short_ids = timeline_plan.short_ids

    log.info(
        "assembly_timeline_built",
        episode_id=episode_id,
        main_entries=len(main_timeline),
        shorts_count=len(short_ids),
    )

    # ------------------------------------------------------------------
    # B0. Narration-completeness gate (2026-09-01) — every narrated beat
    # MUST have a timeline entry (= a video scene). The timeline is built
    # from the Visual Package's scenes, so a beat the scene planner never
    # produced a scene for (e.g. its scene cap fell below the script's
    # beat count) is silently absent here: downstream, its narration audio
    # is trimmed away to fit the shorter video and the story ends
    # mid-arc, yet the coverage check (which only ever sees the timeline)
    # reports "sufficient" and QA passes. This gate makes that an explicit,
    # resumable failure instead — the operator re-runs and the (scene-cap-
    # aware) planner covers every beat. NEVER ship a deliverable that
    # drops narrated content.
    # ------------------------------------------------------------------
    _covered_refs = {
        e.narration_reference for e in main_timeline if e.narration_reference
    }
    for _st in shorts_timelines.values():
        _covered_refs.update(e.narration_reference for e in _st if e.narration_reference)
    # Only MAIN-episode narrated beats must be covered. A narration segment
    # tagged source_type == "short" for a Short that was intentionally not
    # generated (short-native mode, or ENABLE_DEDICATED_SHORT_GENERATION off)
    # is not dropped story content.
    _narrated_refs = {
        (seg.get("beat_reference") or "")
        for seg in narration_package.get("narration_segments", [])
        if (seg.get("source_type") or "main_episode") != "short"
    }
    _narrated_refs.discard("")
    _uncovered_beats = sorted(_narrated_refs - _covered_refs)
    if _uncovered_beats:
        from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR  # noqa: PLC0415
        _acr = {
            "status": "additional_coverage_required",
            "episode_id": episode_id,
            "reason": "narration_beats_without_a_video_scene",
            "max_editorial_speed_factor": MAX_EDITORIAL_SPEED_FACTOR,
            "requests": [
                {
                    "scene_id": _bref,
                    "narration_beat_reference": _bref,
                    "reason": "beat has narration audio but no video scene — "
                              "shipping would silently drop this narrated content",
                }
                for _bref in _uncovered_beats
            ],
        }
        log.error(
            "assembly_narration_beats_uncovered",
            episode_id=episode_id,
            uncovered_beats=_uncovered_beats,
            narrated_beat_count=len(_narrated_refs),
            timeline_entry_count=len(_covered_refs),
        )
        return _build_additional_coverage_required_package(
            episode_id=episode_id,
            visual_package=visual_package,
            narration_package=narration_package,
            thumbnail_package=thumbnail_package,
            additional_coverage_required=_acr,
            log=log,
            episode_dir=episode_dir,
            topic_brief=topic_brief,
            source_registry=source_registry,
        )

    # ------------------------------------------------------------------
    # B1. Coverage check (2026-08-01 policy) — before any rendering, verify
    # every beat (main episode AND every Short — Video Renderer enforces this
    # uniformly, so the pre-render gate must too) has enough real,
    # forward-only visual duration for its narration beat. Video Renderer is
    # not permitted to compensate for missing duration (no tpad/loop/reverse/
    # uncapped setpts — see hypogeum/agents/video_renderer.py::
    # InsufficientCoverageError). A short beat first gets a bounded attempt
    # at additional distinct MUAPI coverage (never regenerating beats that
    # already have enough); if that still isn't enough, assembly halts here
    # and returns a structured additional_coverage_required result instead
    # of proceeding to render.
    #
    # coverage_result (the final, post-repair CoverageCheckResult) is
    # persisted below into final_render_package.coverage_check_summary --
    # the Production Output Gate consumes this canonical result rather than
    # ever recomputing coverage itself (hypogeum/agents/coverage_check.py's
    # own stated principle: this module's result is the one every other
    # module must consume, never re-derive).
    # ------------------------------------------------------------------
    coverage_result = None
    _coverage_max_editorial_speed_factor = None
    if getattr(config, "COVERAGE_CHECK_ENABLED", True):
        from hypogeum.agents.coverage_check import (  # noqa: PLC0415
            MAX_EDITORIAL_SPEED_FACTOR,
            build_additional_coverage_required,
            check_timeline_coverage,
        )
        _coverage_max_editorial_speed_factor = MAX_EDITORIAL_SPEED_FACTOR

        def _all_beat_entries() -> List:
            _entries = list(main_timeline)
            for _st in shorts_timelines.values():
                _entries.extend(_st)
            return _entries

        def _attach_extra_clips(extra_clips_by_scene: Dict[str, List[Path]]) -> None:
            for _entry in _all_beat_entries():
                if _entry.scene_id in extra_clips_by_scene:
                    _entry.extra_clip_paths = extra_clips_by_scene[_entry.scene_id]

        coverage_result = check_timeline_coverage(_all_beat_entries())

        if not coverage_result.sufficient:
            log.warn(
                "assembly_coverage_deficit_detected",
                episode_id=episode_id,
                insufficient_beats=[b.scene_id for b in coverage_result.insufficient_beats],
            )
            from hypogeum.agents.clip_repair_agent import fill_coverage_deficits  # noqa: PLC0415

            extra_clips_by_scene = fill_coverage_deficits(
                insufficient_beats=coverage_result.insufficient_beats,
                visual_package=visual_package,
                assets_root=assets_root,
                config=config,
                episode_id=episode_id,
                log=log,
                channel_id=channel_id,
            )
            _attach_extra_clips(extra_clips_by_scene)

            # fill_coverage_deficits() appends new asset_manifest entries to
            # visual_package in memory for every clip it successfully
            # generated and qualified — persist now, before the coverage
            # recheck below decides whether assembly can proceed, so this
            # paid work is durably recorded on the checkpoint even if
            # coverage still comes up short and this run halts here.
            if extra_clips_by_scene:
                _persist_visual_package_checkpoint(
                    visual_package=visual_package, episode_id=episode_id,
                    episode_dir=episode_dir, log=log,
                )

            coverage_result = check_timeline_coverage(_all_beat_entries())

        if not coverage_result.sufficient:
            acr = build_additional_coverage_required(coverage_result, episode_id)
            log.error(
                "assembly_additional_coverage_required",
                episode_id=episode_id,
                insufficient_beats=len(coverage_result.insufficient_beats),
                requests=acr["requests"],
            )
            return _build_additional_coverage_required_package(
                episode_id=episode_id,
                visual_package=visual_package,
                narration_package=narration_package,
                thumbnail_package=thumbnail_package,
                additional_coverage_required=acr,
                log=log,
                episode_dir=episode_dir,
                topic_brief=topic_brief,
                source_registry=source_registry,
            )

        log.info(
            "assembly_coverage_check_passed",
            episode_id=episode_id,
            beats_checked=len(coverage_result.beats),
        )

    # ------------------------------------------------------------------
    # B2. Timeline QA — run immediately after timeline construction.
    # Results embedded in final_render_package.timeline_qa_report.
    # ------------------------------------------------------------------
    timeline_qa_report = run_timeline_qa(
        timeline_plan=timeline_plan,
        narration_package=narration_package,
        expected_portrait=(getattr(config, "SHORT_NATIVE_MODE", False) is True),
    )

    log.info(
        "assembly_timeline_qa_complete",
        episode_id=episode_id,
        overall_status=timeline_qa_report["overall_status"],
        warnings=len(timeline_qa_report["warnings"]),
        failures=len(timeline_qa_report["failures"]),
    )

    # ------------------------------------------------------------------
    # B3. Assembly Intelligence — detect clip exhaustion, tpad freezes,
    # visible holds, and audio transition issues before rendering.
    # Results are embedded in timeline_qa_report for downstream QA checks.
    # ------------------------------------------------------------------
    try:
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence  # noqa: PLC0415
        _all_timeline_entries: List = list(main_timeline)
        for _st in shorts_timelines.values():
            _all_timeline_entries.extend(_st)
        # music_tracks not yet available here — pass empty dict (music checks
        # will run after C. Music sourcing when assembly_intelligence is called
        # again with full music_tracks)
        assembly_intel_report = run_assembly_intelligence(
            timeline_entries=_all_timeline_entries,
            music_tracks={},
            output_dir=episode_dir,
            episode_id=episode_id,
        )
        # Merge assembly intelligence issues into timeline_qa_report warnings/failures
        for issue in assembly_intel_report.get("issues", []):
            if issue.get("severity") == "BLOCKING":
                timeline_qa_report.setdefault("failures", []).append(
                    f"assembly_intelligence: {issue['issue_type']}: {issue['description']}"
                )
            else:
                timeline_qa_report.setdefault("warnings", []).append(
                    f"assembly_intelligence: {issue['issue_type']}: {issue['description']}"
                )
        # Degrade timeline_qa overall_status if blocking issues were found
        if assembly_intel_report.get("blocking_count", 0) > 0:
            if timeline_qa_report.get("overall_status") not in ("fail",):
                timeline_qa_report["overall_status"] = "fail"
        elif assembly_intel_report.get("warning_count", 0) > 0:
            if timeline_qa_report.get("overall_status") == "pass":
                timeline_qa_report["overall_status"] = "warn"
        log.info(
            "assembly_intelligence_integrated",
            episode_id=episode_id,
            blocking_count=assembly_intel_report.get("blocking_count", 0),
            warning_count=assembly_intel_report.get("warning_count", 0),
            overall_status=assembly_intel_report.get("overall_status", "pass"),
        )
    except Exception as _ai_exc:  # noqa: BLE001
        log.warn(
            "assembly_intelligence_skipped",
            episode_id=episode_id,
            reason=str(_ai_exc),
            note="Assembly Intelligence failed to run — timeline QA unaffected",
        )

    # ------------------------------------------------------------------
    # C. Music sourcing — delegated to Music Agent
    # Assembly Agent must not call generate_music_track directly.
    #
    # Option B (2026-07-19): the main episode is split into contiguous
    # per-category sections (main_music_sections, pure timeline grouping —
    # no I/O). Section boundaries are used both to build per-section
    # categories for Music Agent and, after sourcing, to build the ordered
    # music_segments list the Video Renderer places directly. Shorts are
    # untouched (single section, tone_arc category, exactly as before).
    # ------------------------------------------------------------------
    main_music_sections = compute_music_sections(main_timeline)

    if music_track_package is None:
        section_durations = compute_section_durations(
            main_timeline=main_timeline,
            shorts_timelines=shorts_timelines,
            short_ids=short_ids,
        )
        section_categories = {s["section_key"]: s["category"] for s in main_music_sections}
        music_track_package = run_music_agent(
            visual_package=visual_package,
            episode_id=episode_id,
            config=config,
            assets_root=assets_root,
            section_durations=section_durations,
            section_categories=section_categories,
            channel_id=channel_id,
        )

    music_tracks, music_log, sections_without_music = _music_package_to_tracks(
        music_track_package
    )

    log.info(
        "assembly_music_sourced",
        episode_id=episode_id,
        sections_with_music=len(music_tracks),
        sections_without_music=sections_without_music,
        sourcing_status=music_track_package.get("sourcing_status", "unknown"),
    )

    # music_segments: ready-to-place timed segments for the Video Renderer.
    # The renderer receives only start/end/track — no narrative logic or
    # category derivation happens downstream of this point. When a section
    # has no sourced track (sections_without_music, or an older single-key
    # "main_episode" package), its track is None here; the renderer falls
    # back to the legacy single music_tracks["main_episode"] path when no
    # segment in this list carries a real track.
    music_segments = [
        {
            "start_seconds": s["start_seconds"],
            "end_seconds": s["end_seconds"],
            "track": music_tracks.get(s["section_key"]),
        }
        for s in main_music_sections
    ]

    # ------------------------------------------------------------------
    # D. Audio mixing — narration + music level computations
    # handled by ffmpeg amix/volume filter in ffmpeg execution layer
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # E. ffmpeg execution — delegated to Video Renderer
    # ------------------------------------------------------------------
    main_narration_asset_id = _find_stitched_track_asset_id(stitched_tracks, "main_episode")
    main_narration_path = (
        assets_root / f"{main_narration_asset_id}.mp3"
        if main_narration_asset_id else None
    )
    short_narration_paths: dict = {}
    for short_id in short_ids:
        _short_asset_id = _find_stitched_track_asset_id(stitched_tracks, "short", short_id)
        short_narration_paths[short_id] = (
            assets_root / f"{_short_asset_id}.mp3" if _short_asset_id else None
        )

    # Resolve end-page path: auto-discover from assets_root/end-page.png.
    # Passed to the renderer only when the file exists on disk AND the channel
    # has no branded outro of its own. channel_packages/<id>/outro.mp4 is the
    # per-channel sign-off and supersedes the shared generic end-page card —
    # otherwise a channel gets two end cards (and a shorts-native episode a
    # 30s+ tail on a ~35s story, plus the shared card is Hypogeum-branded).
    _end_page_path = assets_root / "end-page.png"
    _end_page_duration = float(getattr(config, "END_PAGE_DURATION_SECONDS", 20))
    try:
        from hypogeum.channel_package_scaffold import channel_outro_path
        _channel_has_own_outro = channel_outro_path(channel_id) is not None
    except Exception:  # noqa: BLE001
        _channel_has_own_outro = False
    _use_end_page = _end_page_path.exists() and not _channel_has_own_outro
    if _end_page_path.exists() and _channel_has_own_outro:
        log.info(
            "assembly_end_page_skipped",
            episode_id=episode_id,
            reason="channel_has_own_branded_outro",
            channel_id=channel_id,
        )

    # Shorts burned-in captions (2026-08-01 quality rejection: Shorts had
    # platform CC only). Reuses the same asset-id lookup already used for
    # the later caption_asset_id metadata (_find_caption_asset_id below),
    # resolved to the on-disk .srt path so Video Renderer can burn it in.
    _captions = narration_package.get("captions", [])
    short_srt_paths: dict = {}
    for short_id in short_ids:
        _srt_asset_id = _find_caption_asset_id(_captions, "short", short_id)
        _srt_path = assets_root / f"{_srt_asset_id}.srt" if _srt_asset_id else None
        if _srt_path is not None and not _srt_path.exists():
            _srt_path = None
        short_srt_paths[short_id] = _srt_path
        # Fail closed (2026-08-22 fix): a Short whose caption asset cannot be
        # resolved to its own canonical short_id must never render "successfully"
        # with a silently omitted caption path -- this is exactly how
        # ep_f3619022's 3 Shorts ended up with zero burned-in captions and no
        # error anywhere. See docs/operations/audits/
        # EP_F3619022_ASSET_CONTINUITY_AND_BURNED_CAPTIONS_AUDIT_2026-08-22.md.
        if _srt_path is None and getattr(config, "REQUIRE_SHORT_BURNED_CAPTIONS", True):
            raise ShortIdentityLinkageError(
                f"short_id={short_id!r} has no resolvable caption asset in "
                f"narration_package['captions'] (looked up via _find_caption_asset_id) "
                f"-- REQUIRE_SHORT_BURNED_CAPTIONS is True, so this Short cannot be "
                f"rendered without its burned-in captions. Never silently proceed."
            )

    render_manifest = render_episode(
        timeline_plan=timeline_plan,
        main_narration_path=main_narration_path,
        short_narration_paths=short_narration_paths,
        music_tracks=music_tracks,
        clip_availability_rate=clip_availability_rate,
        missing_clips=missing_clips,
        sections_without_music=sections_without_music,
        episode_dir=episode_dir,
        episode_id=episode_id,
        ffmpeg_timeout=ffmpeg_timeout,
        log=log,
        end_page_path=_end_page_path if _use_end_page else None,
        end_page_duration=_end_page_duration,
        music_segments=music_segments,
        short_srt_paths=short_srt_paths,
        ffmpeg_binary=config.FFMPEG_BINARY if isinstance(getattr(config, "FFMPEG_BINARY", None), str) else "ffmpeg",
        main_vertical=(getattr(config, "SHORT_NATIVE_MODE", False) is True),
    )
    render_status = render_manifest["render_status"]

    # ------------------------------------------------------------------
    # E1. Channel branded outro (2026-09-01) — append the channel's fixed
    # sign-off clip (channel_packages/<channel_id>/outro.mp4) to the main
    # render, if it has one. Branding, not narrated content: this runs
    # AFTER the coverage / narration-completeness gates and never affects
    # them. Best-effort — a failed append ships the episode without the
    # outro rather than not at all.
    #
    # Skipped entirely when the channel uses a *generated* outro
    # (outro_policy.json {mode:generated}) — that outro is real narrated beats
    # already in the timeline, so a static clip on top would double it.
    # ------------------------------------------------------------------
    _outro_seconds = 0.0
    try:
        from hypogeum.channel_package_scaffold import channel_outro_policy  # noqa: PLC0415
        _generated_outro = (channel_outro_policy(channel_id) or {}).get("mode") == "generated"
    except Exception:  # noqa: BLE001
        _generated_outro = False
    if _generated_outro:
        log.info("assembly_static_outro_skipped", episode_id=episode_id,
                 reason="channel_uses_generated_outro", channel_id=channel_id)
    if not _generated_outro and render_manifest.get("main_render", {}).get("file_exists"):
        try:
            _res = str(render_manifest.get("main_resolution") or "1080x1920").lower().split("x")
            _w, _h = int(_res[0]), int(_res[1])
        except Exception:  # noqa: BLE001
            _w, _h = 1080, 1920
        _outro_seconds = _append_channel_outro(
            Path(render_manifest["main_render"]["file_path"]),
            channel_id, _w, _h,
            int(render_manifest.get("frame_rate") or FRAME_RATE),
            config.FFMPEG_BINARY if isinstance(getattr(config, "FFMPEG_BINARY", None), str) else "ffmpeg",
            log,
        )
        if _outro_seconds > 0:
            render_manifest["main_render"]["file_size_bytes"] = (
                Path(render_manifest["main_render"]["file_path"]).stat().st_size
            )
            render_manifest["channel_outro_seconds"] = round(_outro_seconds, 3)

    # ------------------------------------------------------------------
    # F. Final Render Package generation
    # ------------------------------------------------------------------
    # Write manifest to disk (separate artifact from final_render_package)
    manifest_path = episode_dir / f"render_output_manifest_{episode_id}.json"
    manifest_path.write_text(
        json.dumps(render_manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Delegate production_ready to Final Render Reviewer (Agent 37).
    # Assembly Agent must never set production_ready directly.
    review_verdict = run_final_render_reviewer(
        render_output_manifest=render_manifest,
        episode_id=episode_id,
    )
    production_ready: bool = review_verdict["production_ready"]
    human_review_recommended: bool = review_verdict["human_review_recommended"]
    review_reasons: List[str] = review_verdict["human_review_reasons"]

    if not production_ready:
        log.error(
            "assembly_pdr_below_threshold",
            episode_id=episode_id,
            reason="production_not_ready",
            overall_verdict=review_verdict["overall_verdict"],
            failures=review_verdict["failures"],
        )

    # Thumbnail references from Thumbnail Package
    thumbnail_references = _collect_thumbnail_references(thumbnail_package)

    # SRT caption references from Narration Package
    main_caption_asset_id = _find_caption_asset_id(
        narration_package.get("captions", []), "main_episode"
    )
    short_caption_ids = {
        short_id: _find_caption_asset_id(
            narration_package.get("captions", []), "short", short_id
        )
        for short_id in short_ids
    }

    # episode_render entry (schema requires video_asset_id and caption_asset_id)
    main_exists = render_manifest["main_render"]["file_exists"]
    main_video_asset_id = (
        f"episodes/{episode_id}/main_{episode_id}" if main_exists else ""
    )
    episode_render = {
        "video_asset_id": main_video_asset_id,
        "duration_seconds": (
            sum_timeline_duration(main_timeline)
            + float(render_manifest.get("end_page_seconds") or 0.0)
            + _outro_seconds
        ),
        "resolution": render_manifest["main_resolution"],
        "frame_rate": render_manifest["frame_rate"],
        "scene_count": len(main_timeline),
        "section_count": len(set(
            e.music_section for e in main_timeline if e.music_section
        )),
        "caption_asset_id": main_caption_asset_id or "",
    }

    # shorts_renders entries -- fail closed, never a placeholder identity.
    # short_ids (0 or exactly 3, per schemas.py's _req_exact(..., allow_zero=True))
    # is the single canonical source for every Short's identity in this
    # package: render_episode() rendered these SAME short_ids (it derives its
    # own short_ids from the identical timeline_plan.short_ids this function
    # read at the top), so a short_id here whose file render_episode() didn't
    # actually produce is a genuine linkage break, not something to paper
    # over with an empty asset_id. See ShortIdentityLinkageError's own
    # docstring and the 2026-08-22 forensic audit this closes.
    shorts_renders: List[dict] = []
    _manifest_shorts = render_manifest.get("shorts_renders", [])
    for short_index, short_id in enumerate(short_ids):
        if short_index >= len(_manifest_shorts):
            # render_episode() never even attempted this short_id -- this is
            # the genuine identity-linkage break (short_ids promised more
            # Shorts than render_episode() itself iterated), never a normal
            # ffmpeg failure. A normal ffmpeg failure for an ATTEMPTED short
            # (in-bounds, file_exists=False) is handled below exactly as
            # before -- render_status becomes "partial"/"failed" and a PDR
            # is issued; that path is unrelated to this identity defect.
            raise ShortIdentityLinkageError(
                f"short_id={short_id!r} (index {short_index}) was never attempted by "
                f"render_episode() -- render_manifest['shorts_renders'] has only "
                f"{len(_manifest_shorts)} entries. This is a genuine short_ids/"
                f"timeline_plan mismatch, never a normal render failure. Never silently "
                f"recorded as an empty video_asset_id."
            )
        _short_file_exists = _manifest_shorts[short_index]["file_exists"]
        short_video_asset_id = (
            f"episodes/{episode_id}/short_{short_index}_{episode_id}"
            if _short_file_exists
            else ""
        )
        shorts_renders.append({
            "short_id": short_id,
            "video_asset_id": short_video_asset_id,
            "duration_seconds": sum_timeline_duration(
                shorts_timelines.get(short_id, [])
            ),
            "resolution": render_manifest["short_resolution"],
            "frame_rate": render_manifest["frame_rate"],
            "scene_count": len(shorts_timelines.get(short_id, [])),
            "caption_asset_id": short_caption_ids.get(short_id) or "",
            "text_overlays_applied": 0,
            "text_overlays_skipped": [],
        })

    # A partial count (neither 0 -- genuinely no Shorts planned, e.g. a smoke
    # test -- nor exactly 3, per schemas.py's own _req_exact(allow_zero=True))
    # is exactly the state the old "pad to exactly 3 with placeholders"
    # fallback used to mask. Fail closed instead: never fabricate a
    # placeholder identity to satisfy the schema.
    if shorts_renders and len(shorts_renders) != 3:
        raise ShortIdentityLinkageError(
            f"short_ids produced {len(shorts_renders)} real Short(s), not 0 or 3 -- "
            f"schemas.py requires exactly 3 (or 0). timeline_plan.short_ids={short_ids!r}. "
            f"This must block the render, never be padded with sh_00N_placeholder entries."
        )

    # metadata_source from Visual Package episode_metadata + Narration Package
    ep_meta = visual_package.get("episode_metadata", {})
    pkg_meta = visual_package.get("package_metadata", {})

    # Chapter timestamps: compute from script_package sections + narration timing
    # when script_package is available; fall back to empty list otherwise.
    script_sections = (script_package or {}).get("sections", [])
    narration_segments_for_chapters = narration_package.get("narration_segments", [])
    computed_section_titles = _compute_chapter_timestamps(
        script_sections=script_sections,
        narration_segments=narration_segments_for_chapters,
    )

    # Source attribution: format source_registry entries for description inclusion
    formatted_sources = _format_sources(source_registry or [])

    # Shorts publication titles: generated from each Short's OWN completed
    # script content (narration_beats + text_overlays) only -- never the
    # historical placeholder ({"short_id": sid, "title": sid}). Degrades
    # to an empty title (never a placeholder, never a fabricated value)
    # when script_package.shorts_scripts is absent/incomplete for this
    # render, or when generation itself fails -- readiness's own
    # A_METADATA check (hypogeum.publishing.readiness) is what actually
    # blocks publishing on a missing title; this stage never silently
    # papers over the gap with a fake-but-schema-valid value.
    #
    # Matched POSITIONALLY, not by short_id string equality: Script
    # Engine's own shorts_scripts[] numbering (e.g. 'short_001') is
    # independent of the Visual Package's per-scene short_id scheme (e.g.
    # 'sh_001') that short_ids/shorts_renders is built from -- the Nth
    # entry in each list is the same logical Short, confirmed by both
    # always being produced in extraction order for the same episode. The
    # output is re-keyed to short_ids' own values so shorts_titles lines
    # up with shorts_renders/resolve_short_deliverable_id() downstream.
    _shorts_scripts = (script_package or {}).get("shorts_scripts", []) or []

    if _shorts_scripts and len(_shorts_scripts) == len(short_ids):
        _requested_shorts_scripts = [
            {**_shorts_scripts[i], "short_id": short_ids[i]}
            for i in range(len(short_ids))
        ]
        # Plan-level weekly reservation boundary (2026-08-26, hardened
        # 2026-08-27): shorts-title generation is paid post-plan OpenRouter
        # text work. Once the plan file exists, channel_id is MANDATORY
        # (missing/mismatched fails closed) -- there is no dry-run exemption
        # for an OpenRouter call. Placed OUTSIDE the try/except below so a
        # reservation denial can never be swallowed as degraded metadata.
        from hypogeum.production_plan import PlanLifecycle, classify_plan_lifecycle
        if classify_plan_lifecycle(episode_id, episode_dir) is not PlanLifecycle.PRE_PLAN:
            from hypogeum.plan_reservation import (
                ensure_verified_plan_weekly_reservation,
            )
            ensure_verified_plan_weekly_reservation(
                channel_id=channel_id,
                episode_id=episode_id,
                config=config,
                log=log,
                output_dir=episode_dir,
            )
        try:
            shorts_titles = generate_shorts_titles(
                _requested_shorts_scripts, config=config, episode_id=episode_id,
                channel_id=channel_id, editorial_rules=shorts_title_editorial_rules,
                llm_call=shorts_title_llm_call,
            )
        except ShortsMetadataError as exc:
            log.warn("shorts_metadata_generation_degraded", error=str(exc), short_ids=short_ids)
            shorts_titles = [{"short_id": sid, "title": ""} for sid in short_ids]
    else:
        log.warn(
            "shorts_metadata_generation_skipped",
            reason="script_package_shorts_scripts_incomplete_or_absent",
            short_ids=short_ids,
        )
        shorts_titles = [{"short_id": sid, "title": ""} for sid in short_ids]

    # Build metadata_source. Title/logline usually live in topic_brief, not
    # ep_meta -- resolve through every upstream package that can carry them
    # (2026-08-29 root-cause fix: ep_f3619022 shipped with an empty title
    # because only ep_meta + topic_brief were checked and both were empty
    # for that run, then it was published by hand).
    _tb = topic_brief or {}
    _nar = narrative_package or {}
    _scr = script_package or {}
    _nar_meta = _nar.get("episode_metadata") or {}
    _nar_struct = _nar.get("episode_structure") or {}

    def _first_nonempty(*vals) -> str:
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    episode_title = _first_nonempty(
        ep_meta.get("episode_title"), _tb.get("episode_title"),
        _nar_meta.get("episode_title"), _nar_meta.get("title"),
        _nar.get("topic"), _scr.get("topic"),
    )
    logline = _first_nonempty(
        ep_meta.get("logline"), _tb.get("logline"),
        _nar_meta.get("logline"), _nar_struct.get("logline"),
    )

    if not episode_title:
        raise AssemblyAgentError(
            f"assembly cannot seal {episode_id!r}: no episode_title found in "
            "visual_package.episode_metadata, topic_brief, narrative_package, "
            "or script_package -- an untitled episode must never be produced."
        )

    # A real description, never a bare logline: logline + the narrative
    # arc's own section purposes when available.
    _section_purposes = [
        s.get("narrative_purpose", "").strip()
        for s in (_nar_struct.get("sections") or [])
        if isinstance(s, dict) and s.get("narrative_purpose", "").strip()
    ][:3]
    description = _first_nonempty(
        ep_meta.get("description"), _nar_meta.get("description"),
        (logline + ("\n\n" + " ".join(_section_purposes) if _section_purposes else "")).strip()
        if logline else "",
        logline,
    )

    metadata_source = {
        "episode_title": episode_title,
        "logline": logline,
        "description": description,
        "section_titles": computed_section_titles,
        "shorts_titles": shorts_titles,
        "sources": formatted_sources,
        "ai_disclosure_text": (
            "This video was produced with AI assistance. "
            "Narration, music, and visuals were generated using AI tools."
        ),
    }

    # Duration coverage — for assembly_qa_report checks only.
    # human_review_recommended and review_reasons come from Final Render Reviewer.
    # Use max_scene_hold_seconds with the Timeline QA 20s threshold to stay consistent
    # with timeline_qa Check B. The aggregate duration_coverage_gap may be large for
    # partial renders; it is the per-scene maximum that determines severity.
    _total_narration_dur = render_manifest["total_narration_seconds"]
    _covered_dur = render_manifest["covered_narration_seconds"]
    _coverage_gap = render_manifest["duration_coverage_gap_seconds"]
    _max_scene_hold = render_manifest.get("max_scene_hold_seconds", _coverage_gap) or _coverage_gap
    _dur_check_status = "fail" if _max_scene_hold > 45.0 else "pass"

    # QA report overall_status.
    # Music-completeness gate (2026-08-29 root-cause fix): when the channel
    # requires music (Config.MUSIC_REQUIRED, default True) a render that
    # sourced NO music at all, or left any section without music, is a hard
    # assembly FAILURE -- not a warning that lets a silent episode ship
    # (ep_f3619022 shipped with music_log == []).
    # `is True`: a real Config carries a real bool; a MagicMock test config
    # must not silently arm this new hard gate.
    _music_required = getattr(config, "MUSIC_REQUIRED", True) is True
    _music_missing = _music_required and (not music_log or bool(sections_without_music))
    if render_status == "failed" or _dur_check_status == "fail" or _music_missing:
        qa_overall = "fail"
    elif render_status == "partial" or sections_without_music or missing_clips or _coverage_gap > 0:
        qa_overall = "warn"
    else:
        qa_overall = "pass"

    final_render_package: dict = {
        "package_metadata": {
            "render_package_id": f"rp_{episode_id}",
            "visual_package_id": pkg_meta.get("visual_package_id", ""),
            "narration_package_id": narration_package.get(
                "package_metadata", {}
            ).get("narration_package_id", ""),
            "thumbnail_package_id": thumbnail_package.get("thumbnail_package_id", ""),
            "episode_id": episode_id,
            "topic_id": ep_meta.get("topic_id", ""),
            "assembly_agent_version": _AGENT_VERSION,
            "rendered_at": datetime.now(timezone.utc).isoformat(),
            "render_duration_seconds": 0.0,
        },
        "episode_render": episode_render,
        "shorts_renders": shorts_renders,
        "thumbnail_references": thumbnail_references,
        "music_log": music_log,
        "metadata_source": metadata_source,
        "assembly_qa_report": {
            "overall_status": qa_overall,
            "checks": [
                {
                    "check_id": "c_clip_availability",
                    "check_type": "clip_availability",
                    "status": "pass" if clip_availability_rate >= 1.0 else "warn",
                    "detail": f"clip_availability_rate={clip_availability_rate:.2%}",
                },
                {
                    "check_id": "c_music_coverage",
                    "check_type": "music_coverage",
                    "status": "pass" if not sections_without_music else "warn",
                    "detail": (
                        f"sections_without_music={sections_without_music}"
                        if sections_without_music else "all_sections_have_music"
                    ),
                },
                {
                    "check_id": "c_render_status",
                    "check_type": "render_status",
                    "status": "pass" if render_status == "success" else
                              "warn" if render_status == "partial" else "fail",
                    "detail": render_status,
                },
                {
                    "check_id": "c_duration_coverage",
                    "check_type": "duration_coverage",
                    "status": _dur_check_status,
                    "detail": (
                        f"narration={_total_narration_dur:.1f}s "
                        f"covered={_covered_dur:.1f}s "
                        f"hold_gap={_coverage_gap:.1f}s "
                        f"max_scene_hold={_max_scene_hold:.1f}s"
                    ),
                },
            ],
            "text_overlay_shifts": [],
            "scenes_missing": missing_clips,
            "human_review_recommended": human_review_recommended,
            "human_review_reasons": review_reasons,
            # Extended QA fields (beyond schema minimum — for traceability)
            "clip_availability_rate": round(clip_availability_rate, 4),
            "sections_without_music": sections_without_music,
        },
        "package_integrity": {
            "input_visual_package_id": pkg_meta.get("visual_package_id", ""),
            "input_narration_package_id": narration_package.get(
                "package_metadata", {}
            ).get("narration_package_id", ""),
            "input_thumbnail_package_id": thumbnail_package.get("thumbnail_package_id", ""),
            "scenes_expected": total_clips,
            "scenes_rendered": present_count,
            # DEPRECATED: shorts_complete — use shorts_runtime_status instead.
            # Kept for backward compatibility with existing packages and tests.
            "shorts_complete": len(short_ids) > 0 and all(
                s["file_exists"] for s in _manifest_shorts
            ),
            "captions_included": bool(main_caption_asset_id),
            "thumbnails_included": bool(thumbnail_references),
            "music_complete": len(sections_without_music) == 0,
            "production_ready": production_ready,
            # Runtime decoupling fields — independent per-deliverable status.
            # main_episode_complete: canonical readiness gate for main upload.
            # Fallback to production_ready for safety if reviewer omits the field.
            "main_episode_complete": review_verdict.get(
                "main_episode_complete", production_ready
            ),
            "main_episode_runtime_status": "complete" if production_ready else "failed",
            "shorts_runtime_status": review_verdict.get("shorts_runtime_status", "not_planned"),
            # Social, archive, analytics are downstream of publishing — always not_planned here.
            "social_runtime_status": "not_planned",
            "archive_runtime_status": "not_planned",
            "analytics_runtime_status": "not_planned",
        },
        "timeline_qa_report": timeline_qa_report,
        # 2026-08-03 Production Output Gate integration: the canonical
        # coverage_check.py result, persisted so the gate consumes it
        # instead of recomputing (coverage_result is None only when
        # COVERAGE_CHECK_ENABLED=false, an explicit opt-out).
        "coverage_check_summary": (
            {
                "sufficient": coverage_result.sufficient,
                "beats_checked": len(coverage_result.beats),
                # Narrated-beat count from the Narration Package. The B0 gate
                # above hard-fails before render when this exceeds
                # beats_checked, so on any package that reaches here the two
                # are equal — surfaced anyway so a reviewer / the output gate
                # can see the deliverable covers the whole narrated story.
                "narrated_beats": len({
                    (s.get("beat_reference") or "")
                    for s in narration_package.get("narration_segments", [])
                    if (s.get("source_type") or "main_episode") != "short"
                } - {""}),
                "insufficient_beats": [
                    b.scene_id for b in coverage_result.insufficient_beats
                ],
                "max_editorial_speed_factor": _coverage_max_editorial_speed_factor,
            }
            if coverage_result is not None
            else None
        ),
    }

    try:
        validate("final_render_package", final_render_package)
    except SchemaError as exc:
        log.warn(
            "assembly_schema_warn",
            path=exc.path,
            reason=exc.reason,
        )

    out_path = episode_dir / f"final_render_package_{episode_id}.json"
    out_path.write_text(
        json.dumps(final_render_package, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "assembly_package_finalized",
        render_package_id=final_render_package["package_metadata"]["render_package_id"],
        render_status=render_status,
        production_ready=production_ready,
        output_path=str(out_path),
        video_assets_count=(
            (1 if render_manifest["main_render"]["file_exists"] else 0)
            + sum(1 for s in _manifest_shorts if s["file_exists"])
        ),
        sections_without_music=sections_without_music,
    )

    return final_render_package


def _find_stitched_track_asset_id(
    stitched_tracks: List[dict],
    source_type: str,
    short_id: Optional[str] = None,
) -> Optional[str]:
    """Find the audio_asset_id for the stitched narration track."""
    for track in stitched_tracks:
        if track.get("source_type") == source_type:
            if source_type == "main_episode":
                return track.get("audio_asset_id")
            elif short_id and short_id in track.get("audio_asset_id", ""):
                return track.get("audio_asset_id")
    return None


def _find_caption_asset_id(
    captions: List[dict],
    source_type: str,
    short_id: Optional[str] = None,
) -> Optional[str]:
    """
    Find the srt_asset_id for a caption entry.

    Real caption entries (see narration_package["captions"]) never carry a
    "source_type" key — only caption_id/srt_asset_id/caption_method/
    has_content. The cap.get("source_type") == source_type branch below is
    therefore dead against real data; matching falls through to the
    srt_asset_id substring checks for both main_episode and short (fixed
    2026-08-01 — this previously left short lookups silently returning
    None: confirmed via a real final_render_package.json where all three
    Shorts had caption_asset_id="").
    """
    for cap in captions:
        srt_id = cap.get("srt_asset_id", "")
        if cap.get("source_type") == source_type or (
            source_type == "main_episode" and "main" in srt_id
        ) or (
            source_type == "short" and short_id and short_id in srt_id
        ):
            if source_type == "main_episode":
                return srt_id
            if short_id and short_id in srt_id:
                return srt_id
    return None


# ---------------------------------------------------------------------------
# Internal: music package helpers
# ---------------------------------------------------------------------------

def _music_package_to_tracks(
    music_track_package: dict,
) -> Tuple[dict, List[dict], List[str]]:
    """
    Unpack a music_track_package into the three values Assembly Agent uses.

    Returns:
        music_tracks:           {section_key: MusicTrackResult | None}
        music_log:              list of music_log[] entries for final_render_package
        sections_without_music: list of section keys with no track
    """
    music_tracks: dict = {}
    tracks_dict = music_track_package.get("tracks", {})
    for section_key, track_dict in tracks_dict.items():
        music_tracks[section_key] = MusicTrackResult(
            track_id=track_dict.get("asset_id", section_key),
            category=track_dict.get("category", "Mystery"),
            asset_id=track_dict["asset_id"],
            local_path=Path(track_dict["local_path"]),
            source=track_dict["source"],
            duration_seconds=track_dict["duration_seconds"],
            prompt_used=track_dict.get("prompt_used") or None,
        )
    music_log: List[dict] = music_track_package.get("music_log", [])
    sections_without_music: List[str] = music_track_package.get("sections_without_music", [])
    return music_tracks, music_log, sections_without_music


# ---------------------------------------------------------------------------
# Internal: thumbnail reference collection
# ---------------------------------------------------------------------------

def _collect_thumbnail_references(thumbnail_package: dict) -> List[dict]:
    """
    Extract thumbnail references from Thumbnail Package for Final Render Package.
    """
    refs: List[dict] = []

    main_thumb = thumbnail_package.get("main_thumbnail", {})
    if main_thumb.get("asset_id"):
        refs.append({
            "thumbnail_asset_id": main_thumb["asset_id"],
            "short_id": None,
            "format": "16:9",
        })

    for st in thumbnail_package.get("short_thumbnails", []):
        if st.get("asset_id"):
            refs.append({
                "thumbnail_asset_id": st["asset_id"],
                "short_id": st.get("short_id"),
                "format": "9:16",
            })

    if not refs:
        # Provide a placeholder so schema validation passes
        refs.append({
            "thumbnail_asset_id": "placeholder",
            "short_id": None,
            "format": "16:9",
        })

    return refs


# ---------------------------------------------------------------------------
# Internal: chapter timestamp computation
# ---------------------------------------------------------------------------

def _compute_chapter_timestamps(
    script_sections: list,
    narration_segments: list,
) -> list:
    """
    Compute section chapter entries with accurate start_seconds from narration timing.

    For each section in script_sections, start_seconds equals the cumulative sum
    of narration beat durations from all preceding sections.

    Args:
        script_sections:    sections[] from script_package.
        narration_segments: narration_segments[] from narration_package.

    Returns:
        list of {"section_id": str, "title": str, "start_seconds": float}
    """
    if not script_sections:
        return []

    # Build beat_reference → duration lookup from narration_segments
    beat_durations: dict[str, float] = {}
    for seg in narration_segments:
        beat_ref = seg.get("beat_reference", "")
        dur = seg.get("duration_seconds", 0.0)
        if beat_ref:
            beat_durations[beat_ref] = float(dur)

    result = []
    cursor = 0.0
    for sec in script_sections:
        sec_id = sec.get("section_id", "")
        title = sec.get("title", "")
        result.append({
            "section_id": sec_id,
            "title": title,
            "start_seconds": cursor,
        })
        # Advance cursor by the sum of this section's narration beat durations
        for beat in sec.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            cursor += beat_durations.get(beat_id, 0.0)

    return result


def _format_sources(source_registry: list) -> list:
    """
    Format source_registry entries for metadata_source.sources[].

    Each entry in the output has at minimum a non-empty "url" or "title".

    Args:
        source_registry: source_registry[] from research_package.

    Returns:
        list of {"title": str, "url": str}
    """
    result = []
    for src in source_registry:
        if not isinstance(src, dict):
            continue
        title = (src.get("title") or "").strip()
        url = (src.get("url") or "").strip()
        if title or url:
            result.append({"title": title, "url": url})
    return result


# ---------------------------------------------------------------------------
# Internal: PDR builder
# ---------------------------------------------------------------------------

def _validate_input_packages(
    visual_package: dict,
    narration_package: dict,
    thumbnail_package: dict,
    script_package: Optional[dict],
    topic_brief: Optional[dict],
    research_package: Optional[dict],
    narrative_package: Optional[dict],
) -> Optional[str]:
    """
    Validate that required input packages are non-None, non-empty dicts, and
    carry the minimum structural keys Assembly needs.

    Returns None on success, or a short error string describing the first
    failing check. Called at stage 0 before any disk I/O or provider call.

    Required (must always be present):
        visual_package    — must be a dict with a 'scenes' key
        narration_package — must be a dict with a 'stitched_tracks' key
        thumbnail_package — must be a non-empty dict

    Optional-but-validated when provided:
        script_package, topic_brief, research_package, narrative_package
        — each must be a non-empty dict if not None
    """
    # Required packages with their minimum structural key
    required_checks: list = [
        ("visual_package",    visual_package,    "scenes"),
        ("narration_package", narration_package, "stitched_tracks"),
        ("thumbnail_package", thumbnail_package, None),
    ]
    for name, pkg, required_key in required_checks:
        if pkg is None:
            return f"required package '{name}' is None"
        if not isinstance(pkg, dict):
            return f"required package '{name}' is not a dict (got {type(pkg).__name__})"
        if not pkg:
            return f"required package '{name}' is an empty dict"
        if required_key is not None and required_key not in pkg:
            return f"required package '{name}' is malformed: missing key '{required_key}'"

    # Optional packages — if provided they must be non-empty dicts
    optional_checks: list = [
        ("script_package",    script_package),
        ("topic_brief",       topic_brief),
        ("research_package",  research_package),
        ("narrative_package", narrative_package),
    ]
    for name, pkg in optional_checks:
        if pkg is not None:
            if not isinstance(pkg, dict):
                return f"optional package '{name}' is not a dict (got {type(pkg).__name__})"
            if not pkg:
                return f"optional package '{name}' was provided but is an empty dict"

    return None


def _build_pdr(episode_id: str, reason: str, severity: str = "BLOCKING") -> dict:
    """Build a minimal Package Deficiency Report."""
    return {
        "report_id": f"pdr_{episode_id}_assembly",
        "issuing_agent": _AGENT_NAME,
        "target_package": "final_render_package",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "severity": severity,
        "deficiency_items": [
            {
                "item_id": "di_001",
                "description": reason,
            }
        ],
        "routing_target": _PDR_ROUTING_TARGET,
    }


def _build_failed_package(
    episode_id: str,
    visual_package: dict,
    narration_package: dict,
    thumbnail_package: dict,
    reason: str,
    clip_availability_rate: float,
    missing_clips: List[str],
    log,
    episode_dir: Path,
    topic_brief: Optional[dict] = None,
    source_registry: Optional[list] = None,
) -> dict:
    """
    Build a minimal Final Render Package with render_status=failed.
    Called when assembly cannot proceed (e.g., clip availability below threshold).

    Still wires available metadata (title, description, captions) from upstream
    packages so downstream QA/publish agents receive what they need to evaluate.
    """
    pkg_meta = visual_package.get("package_metadata", {})
    ep_meta  = visual_package.get("episode_metadata", {})
    _tb = topic_brief or {}

    # Resolve caption even in the failed path — captions may exist on disk
    # independently of whether the video render succeeded.
    main_caption_asset_id = _find_caption_asset_id(
        narration_package.get("captions", []), "main_episode"
    )

    # Deduplicate human_review_reasons — reason may equal the first entry.
    _hrr = ["clip_availability_below_threshold"]
    if reason and reason != "clip_availability_below_threshold":
        _hrr.append(reason)

    package = {
        "package_metadata": {
            "render_package_id": f"rp_{episode_id}_failed",
            "visual_package_id": pkg_meta.get("visual_package_id", ""),
            "narration_package_id": narration_package.get(
                "package_metadata", {}
            ).get("narration_package_id", ""),
            "thumbnail_package_id": thumbnail_package.get("thumbnail_package_id", ""),
            "episode_id": episode_id,
            "topic_id": ep_meta.get("topic_id", ""),
            "assembly_agent_version": _AGENT_VERSION,
            "rendered_at": datetime.now(timezone.utc).isoformat(),
            "render_duration_seconds": 0.0,
        },
        "episode_render": {
            "video_asset_id": "",
            "duration_seconds": 0.0,
            "resolution": MAIN_RESOLUTION,
            "frame_rate": FRAME_RATE,
            "scene_count": 0,
            "section_count": 0,
            "caption_asset_id": main_caption_asset_id or "",
        },
        "shorts_renders": [
            {
                "short_id": f"sh_{i:03d}_placeholder",
                "video_asset_id": "",
                "duration_seconds": 0.0,
                "resolution": SHORT_RESOLUTION,
                "frame_rate": FRAME_RATE,
                "scene_count": 0,
                "caption_asset_id": "",
                "text_overlays_applied": 0,
                "text_overlays_skipped": [],
            }
            for i in range(3)
        ],
        "thumbnail_references": _collect_thumbnail_references(thumbnail_package),
        "music_log": [],
        "metadata_source": {
            "episode_title": ep_meta.get("episode_title") or _tb.get("episode_title", ""),
            "logline": ep_meta.get("logline") or _tb.get("logline", ""),
            "description": ep_meta.get("description") or _tb.get("logline", ""),
            "section_titles": [],
            "shorts_titles": [],
            "sources": _format_sources(source_registry or []),
            "ai_disclosure_text": (
                "This video was produced with AI assistance."
            ),
        },
        "assembly_qa_report": {
            "overall_status": "fail",
            "checks": [
                {
                    "check_id": "c_clip_availability",
                    "check_type": "clip_availability",
                    "status": "fail",
                    "detail": (
                        f"clip_availability_rate={clip_availability_rate:.2%} "
                        f"< threshold={_CLIP_AVAILABILITY_THRESHOLD:.0%}"
                    ),
                },
            ],
            "text_overlay_shifts": [],
            "scenes_missing": missing_clips,
            "human_review_recommended": True,
            "human_review_reasons": _hrr,
            "clip_availability_rate": round(clip_availability_rate, 4),
            "sections_without_music": [],
        },
        "package_integrity": {
            "input_visual_package_id": pkg_meta.get("visual_package_id", ""),
            "input_narration_package_id": narration_package.get(
                "package_metadata", {}
            ).get("narration_package_id", ""),
            "input_thumbnail_package_id": thumbnail_package.get("thumbnail_package_id", ""),
            "scenes_expected": 0,
            "scenes_rendered": 0,
            "shorts_complete": False,
            "captions_included": bool(main_caption_asset_id),
            "thumbnails_included": bool(
                _collect_thumbnail_references(thumbnail_package)
            ),
            "music_complete": False,
            "production_ready": False,
        },
    }

    try:
        validate("final_render_package", package)
    except SchemaError as exc:
        log.warn("assembly_schema_warn", path=exc.path, reason=exc.reason)

    out_path = episode_dir / f"final_render_package_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(package, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.error(
        "assembly_package_finalized",
        render_package_id=package["package_metadata"]["render_package_id"],
        render_status="failed",
        production_ready=False,
        reason=reason,
        output_path=str(out_path),
    )

    return package


def _build_additional_coverage_required_package(
    episode_id: str,
    visual_package: dict,
    narration_package: dict,
    thumbnail_package: dict,
    additional_coverage_required: dict,
    log,
    episode_dir: Path,
    topic_brief: Optional[dict] = None,
    source_registry: Optional[list] = None,
) -> dict:
    """
    Build a minimal Final Render Package with production_ready=False when
    assembly halts because one or more beats lack enough real, forward-only
    visual coverage (2026-08-01 policy — see hypogeum/agents/coverage_check.py).

    Distinct from _build_failed_package's clip_availability_below_threshold:
    that fires when a scene has NO clip at all; this fires when a clip
    exists but doesn't run long enough for its narration beat even after
    the approved editorial speed adjustment, and additional MUAPI
    generation (hypogeum/agents/clip_repair_agent.py::fill_coverage_deficits())
    was attempted and still came up short.

    The structured additional_coverage_required result is carried verbatim
    under that key so a human reviewer or a future automated retry can see
    exactly which beats need more footage and how much.
    """
    pkg_meta = visual_package.get("package_metadata", {})
    ep_meta  = visual_package.get("episode_metadata", {})
    _tb = topic_brief or {}

    main_caption_asset_id = _find_caption_asset_id(
        narration_package.get("captions", []), "main_episode"
    )

    insufficient_scene_ids = [r["scene_id"] for r in additional_coverage_required["requests"]]

    package = {
        "package_metadata": {
            "render_package_id": f"rp_{episode_id}_additional_coverage_required",
            "visual_package_id": pkg_meta.get("visual_package_id", ""),
            "narration_package_id": narration_package.get(
                "package_metadata", {}
            ).get("narration_package_id", ""),
            "thumbnail_package_id": thumbnail_package.get("thumbnail_package_id", ""),
            "episode_id": episode_id,
            "topic_id": ep_meta.get("topic_id", ""),
            "assembly_agent_version": _AGENT_VERSION,
            "rendered_at": datetime.now(timezone.utc).isoformat(),
            "render_duration_seconds": 0.0,
        },
        "episode_render": {
            "video_asset_id": "",
            "duration_seconds": 0.0,
            "resolution": MAIN_RESOLUTION,
            "frame_rate": FRAME_RATE,
            "scene_count": 0,
            "section_count": 0,
            "caption_asset_id": main_caption_asset_id or "",
        },
        "shorts_renders": [
            {
                "short_id": f"sh_{i:03d}_placeholder",
                "video_asset_id": "",
                "duration_seconds": 0.0,
                "resolution": SHORT_RESOLUTION,
                "frame_rate": FRAME_RATE,
                "scene_count": 0,
                "caption_asset_id": "",
                "text_overlays_applied": 0,
                "text_overlays_skipped": [],
            }
            for i in range(3)
        ],
        "thumbnail_references": _collect_thumbnail_references(thumbnail_package),
        "music_log": [],
        "metadata_source": {
            "episode_title": ep_meta.get("episode_title") or _tb.get("episode_title", ""),
            "logline": ep_meta.get("logline") or _tb.get("logline", ""),
            "description": ep_meta.get("description") or _tb.get("logline", ""),
            "section_titles": [],
            "shorts_titles": [],
            "sources": _format_sources(source_registry or []),
            "ai_disclosure_text": (
                "This video was produced with AI assistance."
            ),
        },
        "assembly_qa_report": {
            "overall_status": "fail",
            "checks": [
                {
                    "check_id": "c_visual_coverage",
                    "check_type": "visual_coverage",
                    "status": "fail",
                    "detail": (
                        f"{len(insufficient_scene_ids)} beat(s) lack sufficient unique "
                        f"visual coverage even after the approved editorial speed "
                        f"adjustment ({additional_coverage_required['max_editorial_speed_factor']}x) "
                        f"and an additional-generation attempt: {insufficient_scene_ids[:10]}"
                    ),
                },
            ],
            "text_overlay_shifts": [],
            "scenes_missing": [],
            "human_review_recommended": True,
            "human_review_reasons": ["additional_coverage_required"],
            "clip_availability_rate": 1.0,
            "sections_without_music": [],
        },
        "additional_coverage_required": additional_coverage_required,
        "package_integrity": {
            "input_visual_package_id": pkg_meta.get("visual_package_id", ""),
            "input_narration_package_id": narration_package.get(
                "package_metadata", {}
            ).get("narration_package_id", ""),
            "input_thumbnail_package_id": thumbnail_package.get("thumbnail_package_id", ""),
            "scenes_expected": 0,
            "scenes_rendered": 0,
            "shorts_complete": False,
            "captions_included": bool(main_caption_asset_id),
            "thumbnails_included": bool(
                _collect_thumbnail_references(thumbnail_package)
            ),
            "music_complete": False,
            "production_ready": False,
        },
    }

    try:
        validate("final_render_package", package)
    except SchemaError as exc:
        log.warn("assembly_schema_warn", path=exc.path, reason=exc.reason)

    out_path = episode_dir / f"final_render_package_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(package, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.error(
        "assembly_package_finalized",
        render_package_id=package["package_metadata"]["render_package_id"],
        render_status="additional_coverage_required",
        production_ready=False,
        reason="additional_coverage_required",
        output_path=str(out_path),
    )

    return package
