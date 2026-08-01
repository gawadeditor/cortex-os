#!/usr/bin/env python3
"""
rerender_ep.py — Re-render a single episode from existing clips.

Usage:
    python3 rerender_ep.py --episode-id ep_20260629075717_c7abfd

What it does:
  1. Loads narration_package and visual_package from the episode folder.
  2. Rebuilds the timeline from existing clips and narration beats.
  3. Renders the main video using the fixed video_renderer (trim + setpts).
  4. Appends the end page.
  5. Runs Final Output QA (media-level, not JSON-metadata).
  6. Writes a re-render QA report to the episode folder.

Does NOT:
  - Generate new clips.
  - Upload to YouTube.
  - Spend any credits.

Respects MUAPI_DRY_RUN=true and YOUTUBE_DRY_RUN=true from .env.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent))

from hypogeum.agents.final_output_qa import run_final_output_qa
from hypogeum.agents.timeline_builder import build_timelines, sum_timeline_duration
from hypogeum.agents.video_renderer import render_episode
from hypogeum.config import Config
from hypogeum.logger import get_logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

END_PAGE_DURATION = 20.0
FFMPEG_TIMEOUT    = 900


def main():
    parser = argparse.ArgumentParser(description="Re-render episode from existing clips.")
    parser.add_argument("--episode-id", required=True, help="Episode ID, e.g. ep_20260629075717_c7abfd")
    parser.add_argument("--dry-run", action="store_true", help="Print ffmpeg command but don't execute")
    args = parser.parse_args()

    episode_id = args.episode_id
    config     = Config()
    log        = get_logger()

    assets_root  = Path(__file__).resolve().parent / ".hypogeum-assets"
    episode_dir  = assets_root / "episodes" / episode_id

    if not episode_dir.exists():
        print(f"ERROR: Episode folder not found: {episode_dir}", file=sys.stderr)
        sys.exit(1)

    log.info("rerender_start", episode_id=episode_id, episode_dir=str(episode_dir))

    # ------------------------------------------------------------------
    # 1. Load packages
    # ------------------------------------------------------------------
    vp_path  = episode_dir / f"visual_package_{episode_id}.json"
    np_path  = episode_dir / f"narration_package_{episode_id}.json"

    if not vp_path.exists():
        print(f"ERROR: visual_package not found: {vp_path}", file=sys.stderr)
        sys.exit(1)
    if not np_path.exists():
        print(f"ERROR: narration_package not found: {np_path}", file=sys.stderr)
        sys.exit(1)

    with open(vp_path) as f:
        visual_package = json.load(f)
    with open(np_path) as f:
        narration_package = json.load(f)

    scenes            = visual_package.get("scenes", [])
    narration_segments = narration_package.get("narration_segments", [])

    print(f"Scenes: {len(scenes)}  Narration segments: {len(narration_segments)}")

    # ------------------------------------------------------------------
    # PRE-RENDER QA: comprehensive VO completeness check
    # ------------------------------------------------------------------
    sp_path = episode_dir / f"script_package_{episode_id}.json"
    script_package = {}
    if sp_path.exists():
        with open(sp_path) as f:
            script_package = json.load(f)

    vo_report = _validate_vo_completeness(
        narration_dir=episode_dir / "narration",
        narration_segments=narration_segments,
        script_package=script_package,
        episode_id=episode_id,
    )

    print("\n--- VO Completeness Report ---")
    print(f"  expected_beats:       {vo_report['expected_beats']}")
    print(f"  generated_segments:   {vo_report['generated_segments']}")
    print(f"  missing_files:        {vo_report['missing_files']}")
    print(f"  zero_duration_files:  {vo_report['zero_duration_files']}")
    print(f"  sum_actual_dur:       {vo_report['sum_actual_dur']:.3f}s")
    print(f"  declared_dur:         {vo_report['declared_dur']:.3f}s")
    print(f"  last_segment_id:      {vo_report['last_segment_id']}")
    print(f"  final_script_ending:  {vo_report['final_script_ending'][:80]}")
    print(f"  vo_complete:          {vo_report['vo_complete']}")
    if vo_report['errors']:
        for e in vo_report['errors']:
            print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ VO complete\n")

    # ------------------------------------------------------------------
    # 1b. Re-stitch main_narration.mp3 from beat files
    # ------------------------------------------------------------------
    print("Re-stitching main_narration.mp3 from beat files…")
    narr_path_new, stitched_dur = _restitch_narration(
        narration_dir=episode_dir / "narration",
        narration_segments=narration_segments,
        assets_root=assets_root,
        episode_id=episode_id,
    )
    sum_beat_dur = vo_report["sum_actual_dur"]
    # Acrossfade reduces duration by (N-1)*0.030s for N segments; allow for that reduction
    # plus 0.5s extra for rounding/encoding overhead.
    _n_segs = len(narration_segments)
    _xfade_reduction = max(0.0, _n_segs - 1) * 0.030
    _tolerance = 1.0 + _xfade_reduction
    if stitched_dur < sum_beat_dur - _tolerance:
        print(
            f"ERROR: stitched narration {stitched_dur:.3f}s is shorter than sum of beats "
            f"{sum_beat_dur:.3f}s (tolerance {_tolerance:.3f}s)",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  sum_beat_dur={sum_beat_dur:.3f}s  stitched_dur={stitched_dur:.3f}s  ✓\n")

    # ------------------------------------------------------------------
    # 2. Build asset lookup from episode video folder
    # ------------------------------------------------------------------
    video_dir  = episode_dir / "video"
    asset_lookup: dict = {}
    if video_dir.exists():
        for f in video_dir.iterdir():
            # Only primary clips (no _pass*, _repair*, _combined, _original variants)
            if f.suffix == ".mp4" and not any(x in f.stem for x in ("_pass", "_repair", "_combined", "_original")):
                asset_key = f"episodes/{episode_id}/video/{f.stem}"
                asset_lookup[asset_key] = f

    print(f"Asset lookup: {len(asset_lookup)} primary clips")

    # ------------------------------------------------------------------
    # 3. Build timeline
    # ------------------------------------------------------------------
    timeline_plan = build_timelines(
        scenes=scenes,
        narration_segments=narration_segments,
        asset_lookup=asset_lookup,
        assets_root=assets_root,
        episode_id=episode_id,
    )

    main_timeline = timeline_plan.main_timeline
    total_dur     = sum_timeline_duration(main_timeline)
    present_clips = sum(1 for e in main_timeline if e.clip_path is not None and e.clip_path.exists())

    print(f"\nTimeline: {len(main_timeline)} scenes  {present_clips} clips present  {total_dur:.2f}s body")

    # Per-scene breakdown — coverage facts come from coverage_check.py, the
    # single source of truth also used by the actual render below, so this
    # printout can never disagree with what render_episode() is about to do.
    from hypogeum.agents.coverage_check import compute_beat_coverage
    print("\nScene breakdown:")
    for e in main_timeline:
        has = "✓" if (e.clip_path and e.clip_path.exists()) else "✗"
        cov = compute_beat_coverage(e)
        trim = max(0.0, cov.available_duration_seconds - e.duration_seconds)
        flag = (
            f" TRIM={trim:.2f}s" if trim > 0.001
            else (f" DEFICIT={cov.deficit_seconds:.2f}s" if not cov.sufficient else "")
        )
        print(f"  {has} {e.scene_id:6} beat={e.duration_seconds:.2f}s clip={e.clip_duration_seconds:.2f}s{flag}")

    # ------------------------------------------------------------------
    # 4. Find narration and music
    # ------------------------------------------------------------------
    narr_dir  = episode_dir / "narration"
    narr_path = narr_dir / "main_narration.mp3"
    if not narr_path.exists():
        print(f"ERROR: main_narration.mp3 not found: {narr_path}", file=sys.stderr)
        sys.exit(1)

    narr_dur = _probe_duration(narr_path)
    print(f"\nNarration: {narr_path.name}  duration={narr_dur:.3f}s")
    print(f"Timeline duration: {total_dur:.3f}s  (diff={abs(narr_dur - total_dur):.3f}s)")

    # Music track
    music_track = _find_music_track(assets_root, episode_id, narration_package)
    if music_track:
        print(f"Music: {music_track.local_path.name}  duration={_probe_duration(music_track.local_path):.2f}s")
    else:
        print("Music: none found — narration only")
        print("ERROR: background music is required when narration is present.", file=sys.stderr)
        sys.exit(1)

    # End page
    end_page_path = _find_end_page(assets_root)
    if end_page_path:
        print(f"End page: {end_page_path}")
    else:
        print("End page: not found — body only")

    # ------------------------------------------------------------------
    # PRE-RENDER QA: thumbnail check
    # ------------------------------------------------------------------
    thumbnail_path = episode_dir / "thumbnails" / "main.jpg"
    if not thumbnail_path.exists():
        print(f"ERROR: final thumbnail not found: {thumbnail_path}", file=sys.stderr)
        sys.exit(1)
    print(f"Thumbnail: {thumbnail_path.name} ✓")

    if args.dry_run:
        print("\n[DRY RUN] Would render with fixed ffmpeg command. Exiting.")
        return

    # ------------------------------------------------------------------
    # 5. Back up existing main video
    # ------------------------------------------------------------------
    main_mp4 = episode_dir / f"main_{episode_id}.mp4"
    if main_mp4.exists():
        backup = episode_dir / f"main_{episode_id}.broken.mp4"
        if not backup.exists():
            main_mp4.rename(backup)
            print(f"\nBacked up existing render → {backup.name}")
        else:
            main_mp4.unlink()
            print(f"\nRemoved existing render (backup already exists)")

    # ------------------------------------------------------------------
    # 6. Render
    # ------------------------------------------------------------------
    print(f"\nRendering…")

    music_tracks: dict = {}
    if music_track:
        music_tracks["main_episode"] = music_track

    clip_availability_rate = present_clips / len(main_timeline) if main_timeline else 0.0

    render_manifest = render_episode(
        timeline_plan=timeline_plan,
        main_narration_path=narr_path,
        short_narration_paths={},
        music_tracks=music_tracks,
        clip_availability_rate=clip_availability_rate,
        missing_clips=[e.scene_id for e in main_timeline if not (e.clip_path and e.clip_path.exists())],
        sections_without_music=[],
        episode_dir=episode_dir,
        episode_id=episode_id,
        ffmpeg_timeout=FFMPEG_TIMEOUT,
        log=log,
        end_page_path=end_page_path,
        end_page_duration=END_PAGE_DURATION,
    )

    print(f"\nRender status: {render_manifest['render_status']}")
    if render_manifest.get("ffmpeg_errors"):
        print(f"FFmpeg errors: {render_manifest['ffmpeg_errors']}")

    if not main_mp4.exists():
        print("ERROR: main mp4 not produced.", file=sys.stderr)
        sys.exit(1)

    # Debug: probe the final file right now
    final_dur = _probe_duration(main_mp4)
    expected_total = total_dur + END_PAGE_DURATION
    print(f"\nFile probe: {main_mp4.name}")
    print(f"  size:     {main_mp4.stat().st_size // 1024 // 1024}MB")
    print(f"  duration: {final_dur:.3f}s  (expected ≈ {expected_total:.3f}s)")
    if final_dur > expected_total + 10:
        print(f"  WARNING: Still overlong by {final_dur - expected_total:.1f}s")

    # ------------------------------------------------------------------
    # 7. Final Output QA
    # ------------------------------------------------------------------
    print(f"\nRunning Final Output QA…")

    qa_result = run_final_output_qa(
        mp4_path=main_mp4,
        expected_body_duration=total_dur,
        end_page_duration=END_PAGE_DURATION,
        episode_id=episode_id,
        log=log,
    )

    # ------------------------------------------------------------------
    # 8. Comprehensive QA report
    # ------------------------------------------------------------------
    # Determine music_continues_under_end_page from render_manifest
    music_continues = render_manifest.get("music_continues_under_end_page", False)
    music_dur_used   = render_manifest.get("music_duration_used_s", 0.0)

    # vo_complete check: all gates must pass
    vo_complete_final = (
        vo_report["vo_complete"]
        and stitched_dur >= sum_beat_dur - max(1.0, ((len(narration_segments) - 1) * 0.030) + 0.5)
    )

    print("\n" + "="*60)
    print("RE-RENDER QA REPORT")
    print("="*60)
    print(f"render_status:                {render_manifest['render_status']}")
    print()
    print(f"expected_narration_beats:     {vo_report['expected_beats']}")
    print(f"generated_narration_segments: {vo_report['generated_segments']}")
    print(f"stitched_vo_duration:         {stitched_dur:.3f}s")
    print(f"final_script_ending:          {vo_report['final_script_ending'][:100]}")
    print(f"last_narration_segment_id:    {vo_report['last_segment_id']}")
    print(f"vo_complete:                  {'YES' if vo_complete_final else 'NO'}")
    print()
    print(f"music_duration_used:          {music_dur_used:.3f}s")
    print(f"music_continues_under_end_page: {'YES' if music_continues else 'NO'}")
    print()
    print(f"video_duration:               {qa_result['video_duration_s']:.3f}s")
    print(f"audio_duration:               {qa_result['audio_duration_s']:.3f}s")
    print(f"av_mismatch:                  {qa_result['av_mismatch_s']:.3f}s")
    print(f"expected_body:                {qa_result['expected_body_duration_s']:.3f}s")
    print(f"silence_segments:             {len(qa_result['silence_segments'])}")
    print(f"freeze_segments:              {len(qa_result['freeze_segments'])}")
    print(f"body_silence:                 {len(qa_result['body_silence_before_end'])}")
    print(f"body_freeze_long:             {len([f for f in qa_result['body_freeze_before_end'] if f['duration']>10])}")
    print(f"end_page_start:               {qa_result['end_page_start_s']}")
    print(f"end_page_visible:             {qa_result['end_page_visible']}")
    print(f"passed:                       {qa_result['passed']}")

    if qa_result["blockers"]:
        print("\nBLOCKERS:")
        for b in qa_result["blockers"]:
            print(f"  ✗ {b}")

    if qa_result["warnings"]:
        print("\nWARNINGS:")
        for w in qa_result["warnings"]:
            print(f"  ⚠ {w}")

    print("="*60)

    # Write QA report to disk
    qa_report_path = episode_dir / f"rerender_qa_report_{episode_id}.json"
    report = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "episode_id":     episode_id,
        "vo_completeness": {
            "expected_narration_beats":     vo_report["expected_beats"],
            "generated_narration_segments": vo_report["generated_segments"],
            "missing_files":                vo_report["missing_files"],
            "zero_duration_files":          vo_report["zero_duration_files"],
            "sum_actual_dur_s":             round(vo_report["sum_actual_dur"], 3),
            "declared_dur_s":               round(vo_report["declared_dur"], 3),
            "stitched_vo_duration_s":       round(stitched_dur, 3),
            "last_narration_segment_id":    vo_report["last_segment_id"],
            "final_script_ending":          vo_report["final_script_ending"],
            "vo_complete":                  vo_complete_final,
        },
        "music_qa": {
            "music_duration_used_s":          music_dur_used,
            "music_continues_under_end_page": music_continues,
        },
        "render_manifest":  render_manifest,
        "final_output_qa":  qa_result,
    }
    with open(qa_report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nQA report written: {qa_report_path.name}")

    # Gate: must not mark ready unless VO complete AND music continues under end page
    ready = qa_result["passed"] and vo_complete_final and music_continues
    if ready:
        print("\n✓ QA PASSED — ready for manual review before upload")
    else:
        issues = []
        if not qa_result["passed"]:
            issues.append("media QA blockers present")
        if not vo_complete_final:
            issues.append("vo_complete=NO")
        if not music_continues:
            issues.append("music_continues_under_end_page=NO")
        print(f"\n✗ NOT READY — {', '.join(issues)}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip()) if r.returncode == 0 else 0.0
    except Exception:
        return 0.0


def _find_music_track(assets_root: Path, episode_id: str, narration_package: dict):
    """
    Find the best background music track for this episode.

    Scans the channel mystery music folder and picks the LONGEST track
    (excluding recovery/test files shorter than 30s), which is the
    real background music rather than a temporary recovery file.
    """
    from hypogeum.elevenlabs import MusicTrackResult

    music_dir = assets_root / "channel" / "music" / "mystery"
    if not music_dir.exists():
        return None

    # Filter to real production tracks: must be ≥30s and not a recovery/test file.
    best: tuple | None = None  # (duration, path)
    for f in music_dir.glob("*.mp3"):
        # Skip recovery and test placeholders
        if "recovery" in f.stem.lower() or "test" in f.stem.lower():
            continue
        dur = _probe_duration(f)
        if dur < 30.0:
            continue
        if best is None or dur > best[0]:
            best = (dur, f)

    if best is None:
        # Fallback: pick longest file regardless of name/duration
        for f in music_dir.glob("*.mp3"):
            dur = _probe_duration(f)
            if best is None or dur > best[0]:
                best = (dur, f)

    if best is None:
        return None

    dur, f = best
    return MusicTrackResult(
        track_id=f.stem,
        category="Mystery",
        asset_id=f"channel/music/mystery/{f.name}",
        local_path=f,
        source="local",
        duration_seconds=dur,
    )


def _find_end_page(assets_root: Path) -> Path | None:
    """Find the channel end page PNG."""
    candidates = [
        assets_root / "end-page.png",          # primary location used by assembly_agent
        assets_root / "channel" / "end_page.png",
        assets_root / "channel" / "brand" / "end_page.png",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _validate_vo_completeness(
    narration_dir: Path,
    narration_segments: list,
    script_package: dict,
    episode_id: str,
) -> dict:
    """
    Comprehensive VO completeness check.

    Validates:
    - Expected beats (from script_package) == generated segments (from narration_package)
    - Every narration segment file exists on disk
    - Every segment has non-zero actual duration
    - Last segment id and final script ending text for manual inspection

    Returns dict with:
        expected_beats, generated_segments, missing_files, zero_duration_files,
        sum_actual_dur, declared_dur, last_segment_id, final_script_ending,
        vo_complete, errors
    """
    errors = []
    missing_files = []
    zero_duration_files = []

    # Count expected beats from script_package
    expected_beats = 0
    final_script_ending = ""
    all_beats = []
    for section in script_package.get("sections", []):
        beats = section.get("narration_beats", [])
        all_beats.extend(beats)
    expected_beats = len(all_beats)
    if all_beats:
        final_script_ending = all_beats[-1].get("text", "")

    generated_segments = len(narration_segments)

    # Check beat count match.
    # generated < expected is an error (missing VO content).
    # generated > expected is a warning only — the narration was produced from a
    # longer version of the script and is complete; the stale script_package just
    # reflects an earlier revision.
    if expected_beats > 0 and generated_segments < expected_beats:
        errors.append(
            f"Beat count mismatch: script has {expected_beats} beats but "
            f"narration_package only has {generated_segments} segments (missing VO)"
        )

    # Check each segment file
    sum_actual_dur = 0.0
    declared_dur = 0.0
    last_segment_id = ""

    sorted_segments = sorted(
        narration_segments,
        key=lambda s: s.get("beat_reference", s.get("segment_id", "")),
    )

    for seg in sorted_segments:
        seg_id = seg.get("segment_id", "unknown")
        beat_ref = seg.get("beat_reference", seg_id)
        audio_asset_id = seg.get("audio_asset_id", "")
        declared = float(seg.get("duration_seconds", 0.0))
        declared_dur += declared

        # Resolve file path
        if audio_asset_id:
            mp3_path = narration_dir.parent.parent.parent / f"{audio_asset_id}.mp3"
            # Also try relative to narration_dir
            if not mp3_path.exists():
                mp3_path = narration_dir / f"{beat_ref}.mp3"
        else:
            mp3_path = narration_dir / f"{beat_ref}.mp3"

        if not mp3_path.exists():
            missing_files.append(str(mp3_path.name))
            errors.append(f"Missing narration file: {mp3_path.name} (segment {seg_id})")
            continue

        actual_dur = _probe_duration(mp3_path)
        if actual_dur < 0.1:
            zero_duration_files.append(mp3_path.name)
            errors.append(f"Zero/near-zero duration: {mp3_path.name} = {actual_dur:.3f}s (segment {seg_id})")
        else:
            sum_actual_dur += actual_dur

        last_segment_id = seg_id

    vo_complete = (
        len(errors) == 0
        and expected_beats > 0
        and generated_segments >= expected_beats
        and len(missing_files) == 0
        and len(zero_duration_files) == 0
    )

    return {
        "expected_beats":      expected_beats,
        "generated_segments":  generated_segments,
        "missing_files":       missing_files,
        "zero_duration_files": zero_duration_files,
        "sum_actual_dur":      sum_actual_dur,
        "declared_dur":        declared_dur,
        "last_segment_id":     last_segment_id,
        "final_script_ending": final_script_ending,
        "vo_complete":         vo_complete,
        "errors":              errors,
    }


def _restitch_narration(
    narration_dir: Path,
    narration_segments: list,
    assets_root: Path,
    episode_id: str,
) -> tuple[Path, float]:
    """
    Re-concatenate all beat mp3 files into main_narration.mp3.

    Returns (output_path, stitched_duration).
    Raises RuntimeError on failure.
    """
    sorted_segments = sorted(
        narration_segments,
        key=lambda s: s.get("beat_reference", s.get("segment_id", "")),
    )

    beat_files: list[Path] = []
    for seg in sorted_segments:
        beat_ref = seg.get("beat_reference", "")
        audio_asset_id = seg.get("audio_asset_id", "")

        if audio_asset_id:
            mp3_path = assets_root / f"{audio_asset_id}.mp3"
            if not mp3_path.exists():
                mp3_path = narration_dir / f"{beat_ref}.mp3"
        else:
            mp3_path = narration_dir / f"{beat_ref}.mp3"

        if not mp3_path.exists():
            raise RuntimeError(f"Cannot restitch: missing {mp3_path}")
        beat_files.append(mp3_path)

    if not beat_files:
        raise RuntimeError("No beat files found for restitch")

    out_path = narration_dir / "main_narration.mp3"

    # Backup existing if present
    if out_path.exists():
        backup = narration_dir / "main_narration.pre_restitch.mp3"
        if not backup.exists():
            import shutil
            shutil.copy2(out_path, backup)
            print(f"  Backed up existing narration → {backup.name}")

    # Use acrossfade stitching (30ms crossfade) to eliminate dead-air gaps between beats.
    # Falls back to raw concat (-c copy) on failure.
    _XFADE_DUR = 0.030
    cmd: list = ["ffmpeg", "-y"]
    for p in beat_files:
        cmd += ["-i", str(p)]
    if len(beat_files) == 1:
        cmd += ["-c", "copy", str(out_path)]
    else:
        filter_parts = []
        prev_label = "[0:a]"
        for i in range(1, len(beat_files)):
            is_last = (i == len(beat_files) - 1)
            next_label = "[aout]" if is_last else f"[a{i}]"
            filter_parts.append(
                f"{prev_label}[{i}:a]acrossfade=d={_XFADE_DUR}:c1=tri:c2=tri{next_label}"
            )
            prev_label = next_label
        cmd += [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[aout]",
            "-q:a", "4",
            str(out_path),
        ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(f"  WARNING: acrossfade stitch failed, falling back to raw concat: {r.stderr[-200:]}")
        concat_list = narration_dir / "_concat_list.txt"
        with open(concat_list, "w") as f:
            for p in beat_files:
                f.write(f"file '{p}'\n")
        fallback_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(out_path),
        ]
        r2 = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=120)
        if r2.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {r2.stderr[-500:]}")

    stitched_dur = _probe_duration(out_path)
    print(f"  Stitched {len(beat_files)} beats → {out_path.name}  duration={stitched_dur:.3f}s")
    return out_path, stitched_dur


if __name__ == "__main__":
    main()
