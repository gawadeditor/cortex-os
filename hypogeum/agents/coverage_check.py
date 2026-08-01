"""
hypogeum/agents/coverage_check.py — Pre-assembly visual coverage gate.

Approved production policy (2026-08-01, superseding the interim setpts-only
fix in commit 44ae397): the Video Renderer must never compensate for missing
visual duration. No freeze frames, no forward loops, no ping-pong loops, no
reverse playback, no repeated clip within a beat, no excessive slow-motion
used to fill a gap. When a beat's real footage is short, the factory
generates more distinct footage — even at extra MUAPI cost — rather than
disguising the shortfall with a filter.

This module owns the "is there enough real footage" question, computed
BEFORE any rendering:

    narration_duration_seconds   — TimelineEntry.duration_seconds (already
                                    narration-anchored; see timeline_builder.py)
    available_duration_seconds   — sum of the primary clip's real duration
                                    plus every already-attached extra clip's
                                    real duration (TimelineEntry.clip_duration_seconds
                                    + TimelineEntry.extra_clip_paths)
    max_editorial_duration_seconds — available_duration_seconds stretched by
                                    at most MAX_EDITORIAL_SPEED_FACTOR: the
                                    only speed adjustment Video Renderer is
                                    allowed to apply, and only to smooth a
                                    genuinely small mismatch (e.g. a clip
                                    that's 0.3s short of its beat) — never to
                                    fill a real coverage gap.
    deficit_seconds               — max(0, narration - max_editorial)

A beat with deficit_seconds > 0 is insufficient. check_timeline_coverage()
reports every insufficient beat; build_additional_coverage_required()
turns that into the structured result Assembly Agent returns instead of
proceeding to render. hypogeum/agents/clip_repair_agent.py::
fill_coverage_deficits() is the sibling that actually requests more MUAPI
coverage for exactly those beats — this module never calls MUAPI itself.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from hypogeum.agents.timeline_builder import TimelineEntry

# Approved editorial speed-adjustment range (2026-08-01 policy). 1.00 = no
# change. Anything above 1.00 slows the clip down (stretches it in time) to
# smooth a sub-beat mismatch — e.g. a clip that natively runs 7.8s against
# an 8.0s beat. This is a minor sync correction, not a coverage strategy:
# it is capped low enough that it is not perceptible as "slow motion," and
# Video Renderer treats any factor above this ceiling as a hard failure
# rather than silently applying it. Do not raise this value to paper over a
# real coverage gap — request more footage instead.
MIN_EDITORIAL_SPEED_FACTOR = 1.0
MAX_EDITORIAL_SPEED_FACTOR = 1.15


@dataclass
class BeatCoverage:
    """Coverage facts for one timeline beat (one scene slot)."""
    scene_id: str
    source_type: str
    short_id: Optional[str]
    narration_duration_seconds: float
    available_duration_seconds: float
    max_editorial_duration_seconds: float
    deficit_seconds: float
    sufficient: bool


@dataclass
class CoverageCheckResult:
    """Result of checking every beat in a timeline."""
    sufficient: bool
    beats: List[BeatCoverage] = field(default_factory=list)
    insufficient_beats: List[BeatCoverage] = field(default_factory=list)


def _probe_duration_seconds(path: Path) -> float:
    """Real duration of a media file via ffprobe. Returns 0.0 on any failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        val = result.stdout.strip()
        if result.returncode == 0 and val:
            return float(val)
    except Exception:
        pass
    return 0.0


def compute_beat_coverage(entry: TimelineEntry) -> BeatCoverage:
    """
    Compute coverage facts for one TimelineEntry.

    Artifact-compatibility policy (2026-08-01): a clip that matches the
    MUAPI dry-run placeholder signature (hypogeum.artifact_compatibility.
    is_dry_run_clip()) never counts toward available_duration_seconds,
    regardless of what clip_duration_seconds claims — a dry-run artifact
    can never be silently reused for real production. This is the sole
    extra gate a clip must pass here; a clip that's merely unreadable/
    missing already contributes 0.0 via the existing entry.clip_path is
    None / probe-fallback paths. This does not trigger regeneration by
    itself — check_timeline_coverage()/fill_coverage_deficits() decide
    that from the resulting deficit, exactly as for a beat with no clip.
    """
    from hypogeum.artifact_compatibility import is_dry_run_clip  # noqa: PLC0415

    if entry.clip_path is None or is_dry_run_clip(entry.clip_path):
        available = 0.0
    else:
        available = entry.clip_duration_seconds
        for extra_path in entry.extra_clip_paths:
            if not is_dry_run_clip(extra_path):
                available += _probe_duration_seconds(extra_path)

    max_editorial = available * MAX_EDITORIAL_SPEED_FACTOR
    deficit = max(0.0, entry.duration_seconds - max_editorial)

    return BeatCoverage(
        scene_id=entry.scene_id,
        source_type=entry.source_type,
        short_id=entry.short_id,
        narration_duration_seconds=entry.duration_seconds,
        available_duration_seconds=available,
        max_editorial_duration_seconds=max_editorial,
        deficit_seconds=deficit,
        sufficient=deficit <= 0.001,
    )


def check_timeline_coverage(timeline: List[TimelineEntry]) -> CoverageCheckResult:
    """Compute coverage for every beat in a timeline (main or one Short)."""
    beats = [compute_beat_coverage(e) for e in timeline]
    insufficient = [b for b in beats if not b.sufficient]
    return CoverageCheckResult(
        sufficient=len(insufficient) == 0,
        beats=beats,
        insufficient_beats=insufficient,
    )


def build_additional_coverage_required(
    coverage_result: CoverageCheckResult,
    episode_id: str,
) -> dict:
    """
    Structured result returned by Assembly Agent when assembly cannot
    proceed for lack of real visual coverage. Distinct from
    "clip_availability_below_threshold" (a scene has NO clip at all) —
    this fires when a clip exists but doesn't run long enough for its
    narration beat, even after the approved editorial speed adjustment.
    """
    return {
        "status": "additional_coverage_required",
        "episode_id": episode_id,
        "max_editorial_speed_factor": MAX_EDITORIAL_SPEED_FACTOR,
        "requests": [
            {
                "scene_id": b.scene_id,
                "source_type": b.source_type,
                "short_id": b.short_id,
                "narration_duration_seconds": round(b.narration_duration_seconds, 3),
                "available_duration_seconds": round(b.available_duration_seconds, 3),
                "needed_additional_seconds": round(b.deficit_seconds, 3),
                "reason": "insufficient_unique_visual_coverage",
            }
            for b in coverage_result.insufficient_beats
        ],
    }
