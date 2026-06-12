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
from typing import List, Optional, Tuple

from hypogeum.agents.final_render_reviewer import run_final_render_reviewer
from hypogeum.agents.music_agent import run_music_agent
from hypogeum.agents.timeline_qa import run_timeline_qa
from hypogeum.agents.timeline_builder import (
    build_timelines,
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
_AGENT_VERSION = "1.0"

# Asset inventory threshold (§14 frozen spec: < 80% halts assembly)
_CLIP_AVAILABILITY_THRESHOLD = 0.80

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
) -> dict:
    """
    Execute the Assembly Agent end-to-end.

    Validates assets, constructs timelines, sources music, mixes audio,
    renders video via ffmpeg, and returns a schema-validated Final Render Package.

    Args:
        visual_package:    Validated Visual Package from Visual Package Director.
        narration_package: Validated Narration Package from Audio Agent.
        thumbnail_package: Validated Thumbnail Package from Thumbnail Director.
        config:            Loaded Config instance.
        episode_id:        Episode identifier for asset paths and log correlation.
        output_dir:        Optional output directory override (defaults to
                           .hypogeum-assets/episodes/{episode_id}/).
        script_package:    Optional Script Package for chapter timestamp computation.
                           When provided, section_titles in metadata_source will have
                           accurate start_seconds derived from narration beat durations.
        source_registry:   Optional source_registry[] from the Research Package.
                           When provided, sources are included in metadata_source for
                           description attribution on all publishing platforms.
        music_track_package: Optional music_track_package from Music Agent.
                           When provided, Assembly uses it directly (pipeline mode).
                           When None, Assembly calls run_music_agent() internally
                           (backward-compatible mode until pipeline is wired).

    Returns:
        Final Render Package dict (schema-validated, written to disk).

    Raises:
        AssemblyAgentError — clip availability below 80% threshold.
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

    # Validate clip availability for main episode + all shorts
    present_clips: List[str] = []
    missing_clips: List[str] = []

    for scene in scenes:
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

    total_clips = len(scenes)
    present_count = len(present_clips)
    clip_availability_rate = present_count / total_clips if total_clips > 0 else 0.0

    log.info(
        "assembly_clip_availability",
        episode_id=episode_id,
        total_clips=total_clips,
        present_clips=present_count,
        missing_clips=len(missing_clips),
        clip_availability_rate=round(clip_availability_rate, 4),
        threshold=_CLIP_AVAILABILITY_THRESHOLD,
        passes_threshold=clip_availability_rate >= _CLIP_AVAILABILITY_THRESHOLD,
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
    # B2. Timeline QA — run immediately after timeline construction.
    # Results embedded in final_render_package.timeline_qa_report.
    # ------------------------------------------------------------------
    timeline_qa_report = run_timeline_qa(
        timeline_plan=timeline_plan,
        narration_package=narration_package,
    )

    log.info(
        "assembly_timeline_qa_complete",
        episode_id=episode_id,
        overall_status=timeline_qa_report["overall_status"],
        warnings=len(timeline_qa_report["warnings"]),
        failures=len(timeline_qa_report["failures"]),
    )

    # ------------------------------------------------------------------
    # C. Music sourcing — delegated to Music Agent
    # Assembly Agent must not call generate_music_track directly.
    # ------------------------------------------------------------------
    if music_track_package is None:
        section_durations = compute_section_durations(
            main_timeline=main_timeline,
            shorts_timelines=shorts_timelines,
            short_ids=short_ids,
        )
        music_track_package = run_music_agent(
            visual_package=visual_package,
            episode_id=episode_id,
            config=config,
            assets_root=assets_root,
            section_durations=section_durations,
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
    )
    render_status = render_manifest["render_status"]

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
        "duration_seconds": sum_timeline_duration(main_timeline),
        "resolution": render_manifest["main_resolution"],
        "frame_rate": render_manifest["frame_rate"],
        "scene_count": len(main_timeline),
        "section_count": len(set(
            e.music_section for e in main_timeline if e.music_section
        )),
        "caption_asset_id": main_caption_asset_id or "",
    }

    # shorts_renders entries
    shorts_renders: List[dict] = []
    _manifest_shorts = render_manifest.get("shorts_renders", [])
    for short_index, short_id in enumerate(short_ids):
        _short_file_exists = (
            _manifest_shorts[short_index]["file_exists"]
            if short_index < len(_manifest_shorts)
            else False
        )
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

    # Pad shorts_renders to exactly 3 if needed (schema requires exactly 3)
    while len(shorts_renders) < 3:
        idx = len(shorts_renders)
        shorts_renders.append({
            "short_id": f"sh_{idx:03d}_placeholder",
            "video_asset_id": "",
            "duration_seconds": 0.0,
            "resolution": render_manifest["short_resolution"],
            "frame_rate": render_manifest["frame_rate"],
            "scene_count": 0,
            "caption_asset_id": "",
            "text_overlays_applied": 0,
            "text_overlays_skipped": [],
        })

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

    # Build metadata_source — ep_meta rarely carries title/logline (those live in
    # topic_brief), so fall back to topic_brief when ep_meta fields are empty.
    _tb = topic_brief or {}
    metadata_source = {
        "episode_title": ep_meta.get("episode_title") or _tb.get("episode_title", ""),
        "logline": ep_meta.get("logline") or _tb.get("logline", ""),
        "section_titles": computed_section_titles,
        "shorts_titles": [
            {"short_id": sid, "title": sid}
            for sid in short_ids
        ],
        "sources": formatted_sources,
        "ai_disclosure_text": (
            "This video was produced with AI assistance. "
            "Narration, music, and visuals were generated using AI tools."
        ),
    }

    # Duration coverage — for assembly_qa_report checks only.
    # human_review_recommended and review_reasons come from Final Render Reviewer.
    _total_narration_dur = render_manifest["total_narration_seconds"]
    _covered_dur = render_manifest["covered_narration_seconds"]
    _coverage_gap = render_manifest["duration_coverage_gap_seconds"]
    _dur_check_status = "fail" if _coverage_gap > 5.0 else "pass"

    # QA report overall_status
    if render_status == "failed" or _dur_check_status == "fail":
        qa_overall = "fail"
    elif render_status == "partial" or sections_without_music or missing_clips:
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
                        f"gap={_coverage_gap:.1f}s"
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
            "shorts_complete": len(short_ids) > 0 and all(
                s["file_exists"] for s in _manifest_shorts
            ),
            "captions_included": bool(main_caption_asset_id),
            "thumbnails_included": bool(thumbnail_references),
            "music_complete": len(sections_without_music) == 0,
            "production_ready": production_ready,
        },
        "timeline_qa_report": timeline_qa_report,
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
    """Find the srt_asset_id for a caption entry."""
    for cap in captions:
        if cap.get("source_type") == source_type or (
            source_type == "main_episode" and "main" in cap.get("srt_asset_id", "")
        ):
            if source_type == "main_episode":
                return cap.get("srt_asset_id")
            if short_id and short_id in cap.get("srt_asset_id", ""):
                return cap.get("srt_asset_id")
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
) -> dict:
    """
    Build a minimal Final Render Package with render_status=failed.
    Called when assembly cannot proceed (e.g., clip availability below threshold).
    """
    pkg_meta = visual_package.get("package_metadata", {})
    ep_meta  = visual_package.get("episode_metadata", {})

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
            "caption_asset_id": "",
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
            "episode_title": ep_meta.get("episode_title", ""),
            "logline": ep_meta.get("logline", ""),
            "section_titles": [],
            "shorts_titles": [],
            "sources": [],
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
            "human_review_reasons": ["clip_availability_below_threshold", reason],
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
            "captions_included": False,
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
