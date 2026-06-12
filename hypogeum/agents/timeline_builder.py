"""
hypogeum/agents/timeline_builder.py — Timeline Builder (Priority 15 extraction)

Extracted from assembly_agent.py.

Owns:
- TimelineEntry  — domain type: one scene slot in an assembled timeline
- TimelinePlan   — domain type: complete timeline for an episode (main + all Shorts)
- build_timelines()     — public entry point: scenes + narration → TimelinePlan
- sum_timeline_duration()    — public: total duration across a timeline
- compute_section_durations() — public: section durations for Music Agent input
- find_clip_asset_id()   — public: resolve video clip asset_id for a scene
- derive_asset_id()      — public: derive conventional asset_id from scene fields
- Timeline construction rules (narration-anchored sync, hold logic, scene ordering)
- Music category derivation from scene narrative signals

Does NOT own:
- ffmpeg execution (assembly_agent owns this, accepts List[TimelineEntry] from here)
- Asset inventory validation (assembly_agent orchestration)
- Music sourcing (music_agent owns this)
- Narration generation (voice_generation_operator owns this)
- Final Render Package assembly (assembly_agent owns this)

Multi-channel note:
- This module is the fork point for channel-specific timeline strategies.
- YouTube Longform, Shorts, TikTok, Instagram Reels each have distinct duration
  anchors, padding rules, and sync strategies. New channel adapters import and
  specialize build_timelines() without touching Assembly Agent.

Public API:
    build_timelines(
        scenes, narration_segments, asset_lookup, assets_root, episode_id,
    ) -> TimelinePlan

    sum_timeline_duration(timeline: List[TimelineEntry]) -> float
    compute_section_durations(main_timeline, shorts_timelines, short_ids) -> dict
    find_clip_asset_id(scene, asset_lookup, assets_root, episode_id) -> Optional[str]
    derive_asset_id(scene, episode_id) -> str
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass
class TimelineEntry:
    """
    One entry in the episode or Short timeline.
    Represents one scene slot with its associated assets and overlays.

    This is the unit of exchange between the Timeline Builder and every
    downstream consumer (Video Renderer, duration calculations, QA checks).
    """
    scene_index: int
    scene_id: str
    source_type: str              # "main_episode" or "short"
    short_id: Optional[str]       # populated for shorts
    clip_path: Optional[Path]     # None if clip is missing on disk
    narration_path: Optional[Path]
    start_seconds: float
    duration_seconds: float       # narration beat duration (timeline anchor)
    text_overlays: List[dict]     # from Script Package text_overlays[]
    music_section: Optional[str]  # music category for this section
    text_safe_zone: Optional[str]
    clip_duration_seconds: float = 8.0  # native clip duration from production_spec
    narration_reference: Optional[str] = None  # beat_reference used to look up narration segment


@dataclass
class TimelinePlan:
    """
    Complete timeline for one episode: main track + all Short tracks.

    Produced by build_timelines() and consumed by the assembly pipeline.
    The Video Renderer accepts List[TimelineEntry] from this plan.
    Each channel format (YouTube, TikTok, Instagram) will produce its own
    TimelinePlan with channel-specific duration rules.
    """
    main_timeline: List[TimelineEntry]
    shorts_timelines: Dict[str, List[TimelineEntry]]
    short_ids: List[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_timelines(
    scenes: list,
    narration_segments: list,
    asset_lookup: dict,
    assets_root: Path,
    episode_id: str,
) -> TimelinePlan:
    """
    Build the complete episode timeline from scenes and narration segments.

    Narration-anchored sync: each scene's duration_seconds equals the duration
    of its corresponding narration beat. When the narration beat is longer than
    the clip, the clip must be padded (held last frame). When shorter, the clip
    is trimmed. This preserves perfect audio/video sync.

    Args:
        scenes:            All scenes from the Visual Package (main + shorts).
        narration_segments: narration_segments[] from the Narration Package.
        asset_lookup:      Mapping of asset_id → local file Path.
        assets_root:       Root directory for resolving relative asset paths.
        episode_id:        Episode identifier for path derivation.

    Returns:
        TimelinePlan with main_timeline, shorts_timelines dict, and sorted short_ids.
    """
    # Build beat_reference → segment dict for O(1) lookup
    segment_by_beat: dict[str, dict] = {
        seg.get("beat_reference", ""): seg
        for seg in narration_segments
    }

    main_scenes = [s for s in scenes if s.get("source_type") == "main_episode"]
    short_scenes = [s for s in scenes if s.get("source_type") == "short"]

    main_timeline = _build_timeline(
        scenes=main_scenes,
        segment_by_beat=segment_by_beat,
        asset_lookup=asset_lookup,
        assets_root=assets_root,
        episode_id=episode_id,
        short_id=None,
    )

    short_ids = sorted(set(
        s.get("short_id") for s in short_scenes if s.get("short_id")
    ))

    shorts_timelines: Dict[str, List[TimelineEntry]] = {}
    for short_id in short_ids:
        short_scene_list = [s for s in short_scenes if s.get("short_id") == short_id]
        shorts_timelines[short_id] = _build_timeline(
            scenes=short_scene_list,
            segment_by_beat=segment_by_beat,
            asset_lookup=asset_lookup,
            assets_root=assets_root,
            episode_id=episode_id,
            short_id=short_id,
        )

    return TimelinePlan(
        main_timeline=main_timeline,
        shorts_timelines=shorts_timelines,
        short_ids=short_ids,
    )


def sum_timeline_duration(timeline: List[TimelineEntry]) -> float:
    """Sum all scene durations in a timeline. Returns 0.0 for empty timelines."""
    return sum(e.duration_seconds for e in timeline)


def compute_section_durations(
    main_timeline: List[TimelineEntry],
    shorts_timelines: Dict[str, List[TimelineEntry]],
    short_ids: List[str],
) -> dict:
    """
    Compute section duration requirements for Music Agent input.

    Returns {section_key: required_duration_seconds} where:
    - "main_episode" key has a minimum floor of 30.0 seconds.
    - "short_{short_id}" keys have a minimum floor of 10.0 seconds.

    The Music Agent uses these to select tracks of sufficient length.
    """
    durations: dict = {}
    main_dur = sum_timeline_duration(main_timeline)
    durations["main_episode"] = max(main_dur, 30.0)
    for short_id in short_ids:
        tl = shorts_timelines.get(short_id, [])
        dur = sum_timeline_duration(tl)
        durations[f"short_{short_id}"] = max(dur, 10.0)
    return durations


def find_clip_asset_id(
    scene: dict,
    asset_lookup: dict,
    assets_root: Path,
    episode_id: str,
) -> Optional[str]:
    """
    Resolve the asset_id for a scene's video clip.

    Resolution order:
    1. Explicit scene.asset_id field (highest priority).
    2. Lookup by scene_id substring in asset_lookup keys.
    3. Derive conventional path: episodes/{ep}/video/sc_{scene_id} (main)
       or episodes/{ep}/video/sh_{short_id}/sc_{scene_id} (short).

    Returns the asset_id string, or the derived path as a fallback.
    Never returns None — always produces a resolvable path string.
    """
    # Try explicit asset_id on scene
    aid = scene.get("asset_id")
    if aid:
        return aid
    # Try looking up by scene_id in asset_lookup
    scene_id = scene.get("scene_id", "")
    for k in asset_lookup:
        if scene_id in k:
            return k
    # Derive a conventional path
    short_id = scene.get("short_id")
    if short_id:
        return f"episodes/{episode_id}/video/sh_{short_id}/sc_{scene_id}"
    return f"episodes/{episode_id}/video/sc_{scene_id}"


def derive_asset_id(scene: dict, episode_id: str) -> str:
    """
    Derive the conventional asset_id for a scene from its scene_id field.

    Used when a scene has no explicit asset_id and no entry in the asset_lookup.
    Main episode scenes: episodes/{episode_id}/video/sc_{scene_id}
    Short scenes:        episodes/{episode_id}/video/sh_{short_id}/sc_{scene_id}
    """
    short_id = scene.get("short_id")
    scene_id = scene.get("scene_id", "unknown")
    if short_id:
        return f"episodes/{episode_id}/video/sh_{short_id}/sc_{scene_id}"
    return f"episodes/{episode_id}/video/sc_{scene_id}"


# ---------------------------------------------------------------------------
# Internal: per-timeline construction
# ---------------------------------------------------------------------------

def _probe_clip_duration(clip_path: Optional[Path], fallback: float) -> float:
    """
    Return the actual duration of a video file using ffprobe.

    Falls back to `fallback` when:
    - clip_path is None or the file does not exist
    - ffprobe is not available or returns a non-zero exit code
    - The output cannot be parsed as a float

    This ensures hold_seconds in the renderer is computed from the real delivered
    file duration rather than the production_spec requested duration, which may
    differ (e.g. runway-image-to-video delivers 10s for an 8s spec).
    """
    if clip_path is None or not clip_path.exists():
        return fallback
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(clip_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return fallback


def _build_timeline(
    scenes: List[dict],
    segment_by_beat: dict,
    asset_lookup: dict,
    assets_root: Path,
    episode_id: str,
    short_id: Optional[str],
) -> List[TimelineEntry]:
    """
    Build an ordered TimelineEntry list for one timeline (main or one Short).

    Scenes are sorted by scene_index. Duration is narration-anchored:
    beat_duration takes precedence over clip_duration when available.
    """
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_index", 0))
    entries: List[TimelineEntry] = []
    cursor = 0.0

    for scene in sorted_scenes:
        scene_id = scene.get("scene_id", "")
        scene_index = scene.get("scene_index", 0)

        # Determine scene duration from narration beats (narration-anchored sync)
        narration_ref = scene.get("narration_reference", "")
        beat_duration = _get_beat_duration(narration_ref, segment_by_beat)

        # Spec duration from Visual Package (5s or 8s per MUAPI constraints)
        spec_duration = float(scene.get("production_spec", {}).get("duration_seconds", 8.0))

        # Find clip path first — required for actual duration probe below
        asset_id = find_clip_asset_id(scene, asset_lookup, assets_root, episode_id)
        if asset_id:
            candidate = asset_lookup.get(asset_id)
            if candidate is None:
                candidate = assets_root / f"{asset_id}.mp4"
            clip_path = candidate if candidate.exists() else None
        else:
            clip_path = None

        # Actual clip duration from file; falls back to spec when file is absent or probe fails.
        # Providers may deliver clips longer than the requested spec (e.g. runway-image-to-video
        # delivers ~10s for an 8s spec), so hold_seconds must be computed from the real duration.
        clip_duration = _probe_clip_duration(clip_path, fallback=spec_duration)

        # Effective duration: narration-anchored (never trim narration)
        scene_duration = beat_duration if beat_duration > 0 else clip_duration

        # Narration segment path
        narration_path: Optional[Path] = None
        if narration_ref and narration_ref in segment_by_beat:
            seg = segment_by_beat[narration_ref]
            audio_asset_id = seg.get("audio_asset_id", "")
            if audio_asset_id:
                p = assets_root / f"{audio_asset_id}.mp3"
                narration_path = p if p.exists() else None

        # Music section from narrative signals
        music_section = _derive_music_category_for_scene(scene)

        # Text safe zone (Short scenes only)
        text_safe_zone = scene.get("text_safe_zone")

        entries.append(TimelineEntry(
            scene_index=scene_index,
            scene_id=scene_id,
            source_type=scene.get("source_type", "main_episode"),
            short_id=short_id,
            clip_path=clip_path,
            narration_path=narration_path,
            start_seconds=cursor,
            duration_seconds=scene_duration,
            text_overlays=scene.get("text_overlays", []),
            music_section=music_section,
            text_safe_zone=text_safe_zone,
            clip_duration_seconds=clip_duration,
            narration_reference=narration_ref,
        ))
        cursor += scene_duration

    return entries


def _get_beat_duration(narration_ref: str, segment_by_beat: dict) -> float:
    """Get the duration of a narration beat from segment lookup. Returns 0.0 if not found."""
    if not narration_ref:
        return 0.0
    seg = segment_by_beat.get(narration_ref, {})
    return float(seg.get("duration_seconds", 0.0))


def _derive_music_category_for_scene(scene: dict) -> str:
    """
    Derive music category from scene narrative signals.

    Maps the first narrative signal's signal_type to a Music Agent category.
    Falls back to "Mystery" when no signals are present or the signal is unknown.
    """
    signals = scene.get("narrative_signals", [])
    if isinstance(signals, list) and signals:
        first_signal = signals[0]
        signal_type = (
            first_signal if isinstance(first_signal, str)
            else first_signal.get("signal_type", "")
        )
        _signal_to_category = {
            "mystery_deepening": "Mystery",
            "shock_fact":        "Tension",
            "reveal":            "Resolution",
            "discovery":         "Discovery",
            "investigation":     "Investigation",
            "tension":           "Tension",
            "foreshadowing":     "Mystery",
        }
        return _signal_to_category.get(signal_type.lower(), "Mystery")
    return "Mystery"
