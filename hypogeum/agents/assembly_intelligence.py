"""
Assembly Intelligence — detects and prevents assembly errors.

Checks performed:
- music_discontinuity: music cuts or restarts at clip boundaries
- bad_audio_transition: abrupt audio level changes between clips
- end_page_music_restart: music resets to beginning on end-page
- tpad_freeze: clip padded with a frozen hold frame (tpad filter)
- clip_exhaustion: same clip repeated more than 2x
- visible_hold: frame-identical sections indicating a held freeze

Public API:
    run_assembly_intelligence(
        timeline_entries, music_tracks, output_dir, episode_id
    ) → dict

No Claude calls — pure Python analysis of the timeline data.

Logging events:
    assembly_intelligence_started   — info  — before checks
    assembly_intelligence_completed — info  — after checks with overall_status
    assembly_intelligence_blocking  — error — blocking issues found
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DEFAULT_ASSETS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / ".hypogeum-assets"
)

# Maximum number of times a single clip asset_id may appear in the timeline
# before triggering a clip_exhaustion issue.
_CLIP_EXHAUSTION_THRESHOLD = 2

# Minimum accepted audio level difference (amplitude ratio) between
# adjacent clips before triggering a bad_audio_transition warning.
# Not enforced unless the timeline carries explicit audio_level fields.
_AUDIO_LEVEL_JUMP_THRESHOLD = 0.5


def run_assembly_intelligence(
    timeline_entries: list,
    music_tracks: dict,
    output_dir: Optional[Path],
    episode_id: str,
) -> dict:
    """
    Analyse the timeline for assembly errors before or after rendering.

    Args:
        timeline_entries: List of timeline entry dicts (from timeline_builder output).
                          Each entry is expected to have at minimum:
                          - clip_asset_id (str)
                          - scene_id (str)
                          - duration_seconds (float)
                          Optional fields:
                          - audio_level (float)  — amplitude 0.0–1.0
                          - tpad (bool)          — True if tpad freeze filter applied
                          - ffmpeg_filter (str)  — raw ffmpeg filter string
        music_tracks:     Dict of section_key → track dict (from music_track_package.tracks).
                          Used to detect music restart issues.
        output_dir:       Output directory for the checkpoint file.
        episode_id:       Episode identifier for logging and file naming.

    Returns:
        Assembly intelligence result dict:
        {
          "assembly_intelligence_id": str,
          "episode_id": str,
          "evaluated_at": str,
          "issues": [
            {
              "issue_id": str,
              "issue_type": str,
              "severity": "BLOCKING" | "WARNING",
              "description": str,
              "affected_clips": [str],
              "remediation": str
            }
          ],
          "overall_status": "pass" | "warn" | "fail",
          "blocking_count": int,
          "warning_count": int
        }
    """
    try:
        from hypogeum.logger import get_logger  # noqa: PLC0415
        log = get_logger(episode_id=episode_id, agent="assembly_intelligence")
    except Exception:  # noqa: BLE001
        log = _NullLogger()

    _resolve_dir: Path = (
        Path(output_dir) if output_dir is not None
        else _DEFAULT_ASSETS_ROOT / "episodes" / episode_id
    )
    _resolve_dir.mkdir(parents=True, exist_ok=True)

    ckpt_path = _resolve_dir / f"assembly_intelligence_{episode_id}.json"
    if ckpt_path.exists():
        return json.loads(ckpt_path.read_text(encoding="utf-8"))

    log.info(
        "assembly_intelligence_started",
        episode_id=episode_id,
        timeline_entries=len(timeline_entries),
        music_sections=len(music_tracks) if music_tracks else 0,
    )

    issues: list[dict] = []
    issue_counter = 0

    def _next_issue_id() -> str:
        nonlocal issue_counter
        issue_counter += 1
        return f"ai_issue_{issue_counter:03d}"

    # ------------------------------------------------------------------
    # Check: clip_exhaustion
    # Same clip asset_id appears more than _CLIP_EXHAUSTION_THRESHOLD times.
    # BLOCKING — audiences will notice repeated footage.
    # ------------------------------------------------------------------
    clip_id_counter: Counter = Counter()
    for entry in timeline_entries:
        cid = _get_clip_id(entry)
        if cid:
            clip_id_counter[cid] += 1

    for clip_id, count in clip_id_counter.items():
        if count > _CLIP_EXHAUSTION_THRESHOLD:
            issues.append({
                "issue_id": _next_issue_id(),
                "issue_type": "clip_exhaustion",
                "severity": "BLOCKING",
                "description": (
                    f"Clip '{clip_id}' appears {count} times in the timeline "
                    f"(threshold: {_CLIP_EXHAUSTION_THRESHOLD}). "
                    f"Repeated footage is immediately visible to viewers."
                ),
                "affected_clips": [clip_id],
                "remediation": (
                    f"Generate {count - _CLIP_EXHAUSTION_THRESHOLD} additional unique clip(s) "
                    f"for the scenes currently using '{clip_id}'. "
                    f"Use run_clip_repair() with a semantic substitution strategy."
                ),
            })

    # ------------------------------------------------------------------
    # Check: tpad_freeze
    # Timeline entry has tpad=True or ffmpeg_filter contains 'tpad'.
    # WARNING — tpad creates a freeze-frame hold at the end of a clip.
    # ------------------------------------------------------------------
    tpad_clips: list[str] = []
    for entry in timeline_entries:
        cid = _get_clip_id(entry)
        has_tpad = entry.get("tpad", False)
        ffmpeg_filter = entry.get("ffmpeg_filter", "") or ""
        if has_tpad or "tpad" in ffmpeg_filter.lower():
            tpad_clips.append(cid or entry.get("scene_id", "unknown"))

    if tpad_clips:
        issues.append({
            "issue_id": _next_issue_id(),
            "issue_type": "tpad_freeze",
            "severity": "WARNING",
            "description": (
                f"{len(tpad_clips)} clip(s) use tpad freeze padding: "
                f"{', '.join(tpad_clips[:5])}{'...' if len(tpad_clips) > 5 else ''}. "
                f"Freeze-frame holds are visible at clip boundaries."
            ),
            "affected_clips": tpad_clips,
            "remediation": (
                "Replace freeze padding with a hold-frame crossfade or generate a "
                "longer version of the clip. Alternatively use an extended still "
                "image clip rather than tpad."
            ),
        })

    # ------------------------------------------------------------------
    # Check: visible_hold
    # Detected when consecutive timeline entries reference the same clip_id
    # (semantic proxy for frame-identical sections).
    # WARNING (two consecutive), BLOCKING (three or more consecutive).
    # ------------------------------------------------------------------
    consecutive_same_clip_runs: list[tuple[str, int]] = []
    run_cid: Optional[str] = None
    run_len = 0

    for entry in timeline_entries:
        cid = _get_clip_id(entry)
        if not cid:
            run_cid = None
            run_len = 0
            continue
        if cid == run_cid:
            run_len += 1
        else:
            if run_len >= 2 and run_cid:
                consecutive_same_clip_runs.append((run_cid, run_len))
            run_cid = cid
            run_len = 1

    if run_len >= 2 and run_cid:
        consecutive_same_clip_runs.append((run_cid, run_len))

    for cid, run_count in consecutive_same_clip_runs:
        severity = "BLOCKING" if run_count >= 3 else "WARNING"
        issues.append({
            "issue_id": _next_issue_id(),
            "issue_type": "visible_hold",
            "severity": severity,
            "description": (
                f"Clip '{cid}' appears {run_count} times consecutively, "
                f"indicating a visible hold or repeated identical footage block."
            ),
            "affected_clips": [cid],
            "remediation": (
                f"Break up the {run_count} consecutive uses of '{cid}' by "
                f"inserting at least one different clip between repetitions. "
                f"Use clip_repair or generate a variation clip."
            ),
        })

    # ------------------------------------------------------------------
    # Check: bad_audio_transition
    # Adjacent clips with audio_level fields that differ by more than
    # _AUDIO_LEVEL_JUMP_THRESHOLD (amplitude ratio).
    # WARNING only — audio mixing can compensate.
    # ------------------------------------------------------------------
    audio_transition_issues: list[str] = []
    for i in range(len(timeline_entries) - 1):
        lvl_a = timeline_entries[i].get("audio_level")
        lvl_b = timeline_entries[i + 1].get("audio_level")
        if lvl_a is not None and lvl_b is not None:
            try:
                diff = abs(float(lvl_a) - float(lvl_b))
                if diff > _AUDIO_LEVEL_JUMP_THRESHOLD:
                    cid_a = _get_clip_id(timeline_entries[i]) or f"entry_{i}"
                    cid_b = _get_clip_id(timeline_entries[i + 1]) or f"entry_{i+1}"
                    audio_transition_issues.append(
                        f"{cid_a}→{cid_b} (diff={diff:.2f})"
                    )
            except (TypeError, ValueError):
                pass

    if audio_transition_issues:
        issues.append({
            "issue_id": _next_issue_id(),
            "issue_type": "bad_audio_transition",
            "severity": "WARNING",
            "description": (
                f"{len(audio_transition_issues)} audio level jump(s) detected at "
                f"clip boundaries: {', '.join(audio_transition_issues[:3])}"
                f"{'...' if len(audio_transition_issues) > 3 else ''}"
            ),
            "affected_clips": [
                t.split("→")[0] for t in audio_transition_issues
            ],
            "remediation": (
                "Apply audio fade-out/fade-in transitions at clip boundaries with "
                "large level differences. Normalise clip audio levels before mixing."
            ),
        })

    # ------------------------------------------------------------------
    # Check: music_discontinuity
    # Detected when music_tracks is empty or sections are missing tracks.
    # WARNING — music absence creates audible gaps.
    # ------------------------------------------------------------------
    if not music_tracks:
        issues.append({
            "issue_id": _next_issue_id(),
            "issue_type": "music_discontinuity",
            "severity": "WARNING",
            "description": "No music tracks provided. Episode will have no background music.",
            "affected_clips": [],
            "remediation": "Run the Music Agent to source background music for all sections.",
        })

    # ------------------------------------------------------------------
    # Check: end_page_music_restart
    # Detected when any track has section_key containing 'end' and its
    # local_path differs from the main episode track — indicating a fresh
    # music start rather than a continuation.
    # WARNING — audible if the end card has noticeably different music.
    # ------------------------------------------------------------------
    if music_tracks:
        main_tracks = [
            v for k, v in music_tracks.items()
            if "end" not in k.lower() and "outro" not in k.lower()
        ]
        end_tracks = [
            v for k, v in music_tracks.items()
            if "end" in k.lower() or "outro" in k.lower()
        ]
        if main_tracks and end_tracks:
            main_assets = {t.get("asset_id", "") for t in main_tracks}
            end_assets = {t.get("asset_id", "") for t in end_tracks}
            if not main_assets.intersection(end_assets):
                issues.append({
                    "issue_id": _next_issue_id(),
                    "issue_type": "end_page_music_restart",
                    "severity": "WARNING",
                    "description": (
                        "End-page music uses different asset_id than main episode music, "
                        "which may cause a noticeable restart or discontinuity at the "
                        "episode's end card."
                    ),
                    "affected_clips": list(end_assets),
                    "remediation": (
                        "Use the same music asset as the main episode body for the end "
                        "card, or fade the main track out smoothly into the end card "
                        "rather than starting a new track."
                    ),
                })

    # ------------------------------------------------------------------
    # Compute overall status
    # ------------------------------------------------------------------
    blocking_count = sum(1 for issue in issues if issue["severity"] == "BLOCKING")
    warning_count = sum(1 for issue in issues if issue["severity"] == "WARNING")

    if blocking_count > 0:
        overall_status = "fail"
    elif warning_count > 0:
        overall_status = "warn"
    else:
        overall_status = "pass"

    result = {
        "assembly_intelligence_id": f"ai_{episode_id}_{uuid.uuid4().hex[:8]}",
        "episode_id": episode_id,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "issues": issues,
        "overall_status": overall_status,
        "blocking_count": blocking_count,
        "warning_count": warning_count,
    }

    if blocking_count > 0:
        log.error(
            "assembly_intelligence_blocking",
            episode_id=episode_id,
            blocking_count=blocking_count,
            blocking_types=[
                i["issue_type"] for i in issues if i["severity"] == "BLOCKING"
            ],
        )
    else:
        log.info(
            "assembly_intelligence_completed",
            episode_id=episode_id,
            overall_status=overall_status,
            blocking_count=blocking_count,
            warning_count=warning_count,
        )

    ckpt_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_clip_id(entry: Any) -> Optional[str]:
    """Extract clip asset_id from a timeline entry dict, trying multiple field names."""
    if not isinstance(entry, dict):
        return None
    return (
        entry.get("clip_asset_id")
        or entry.get("asset_id")
        or entry.get("clip_id")
        or None
    )


class _NullLogger:
    """Minimal logger shim used when hypogeum.logger is unavailable (tests)."""

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warn(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass
