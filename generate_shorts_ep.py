#!/usr/bin/env python3
"""
generate_shorts_ep.py — Generate 5 Shorts from existing episode assets.

Assembles 5 Shorts at 1080x1920 (9:16) by:
  1. Concatenating selected narration beat files (no new TTS needed).
  2. Using corresponding main episode clips with 9:16 center crop.
  3. Mixing narration + background music at -18dB.
  4. Appending end-page card to each short.

Usage:
    python3 generate_shorts_ep.py --episode-id ep_20260629075717_c7abfd
    python3 generate_shorts_ep.py --episode-id ep_20260629075717_c7abfd --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hypogeum.agents.final_output_qa import run_final_output_qa
from hypogeum.config import Config
from hypogeum.logger import get_logger

# ---------------------------------------------------------------------------
# Short definitions — 5 shorts from the existing 33 scenes
# Each selects narration beats + corresponding scene clips.
# ---------------------------------------------------------------------------

SHORTS = [
    {
        "short_id": "sh_001",
        "title": "The Hidden City Beneath the Colosseum",
        "beats": ["b_001", "b_002", "b_003", "b_004", "b_005"],
        "scenes": ["s01", "s02", "s03", "s04", "s05"],
    },
    {
        "short_id": "sh_002",
        "title": "Underground Arena: What Lived Below",
        "beats": ["b_006", "b_007", "b_008", "b_009"],
        "scenes": ["s06", "s07", "s08", "s09"],
    },
    {
        "short_id": "sh_003",
        "title": "The Hypogeum Revealed",
        "beats": ["b_011", "b_012", "b_013"],
        "scenes": ["s11", "s12", "s13"],
    },
    {
        "short_id": "sh_004",
        "title": "Life and Death Beneath the Arena",
        "beats": ["b_016", "b_017", "b_018"],
        "scenes": ["s16", "s17", "s18"],
    },
    {
        "short_id": "sh_005",
        "title": "The Colosseum's Last Secret",
        "beats": ["b_030", "b_031", "b_032", "b_033"],
        "scenes": ["s30", "s31", "s32", "s33"],
    },
]

SHORT_RESOLUTION = "1080x1920"
FRAME_RATE       = 30
VIDEO_CODEC      = "libx264"
AUDIO_CODEC      = "aac"
AUDIO_BITRATE    = "128k"
CRF              = "23"
MUSIC_FACTOR     = 0.126   # -18 dB
END_PAGE_DURATION = 10.0   # shorter end-page for shorts
FFMPEG_TIMEOUT   = 600


def main():
    parser = argparse.ArgumentParser(description="Generate 5 Shorts from existing episode assets.")
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Show plan without rendering")
    args = parser.parse_args()

    episode_id = args.episode_id
    log = get_logger()
    assets_root = Path(__file__).resolve().parent / ".hypogeum-assets"
    episode_dir = assets_root / "episodes" / episode_id

    if not episode_dir.exists():
        print(f"ERROR: Episode folder not found: {episode_dir}", file=sys.stderr)
        sys.exit(1)

    narration_dir = episode_dir / "narration"
    video_dir     = episode_dir / "video"
    shorts_dir    = episode_dir / "shorts"
    shorts_dir.mkdir(exist_ok=True)

    # Find music track (longest production track)
    music_path = _find_music_track(assets_root)
    if not music_path:
        print("ERROR: No background music track found.", file=sys.stderr)
        sys.exit(1)
    print(f"Music: {music_path.name}  {_probe_duration(music_path):.1f}s")

    # Find end page
    end_page_path = _find_end_page(assets_root)
    print(f"End page: {end_page_path}")

    # ---------------------------------------------------------------------------
    # Plan + render each short
    # ---------------------------------------------------------------------------
    results = []
    for short_def in SHORTS:
        short_id = short_def["short_id"]
        title    = short_def["title"]
        beats    = short_def["beats"]
        scenes   = short_def["scenes"]

        print(f"\n{'='*60}")
        print(f"Short: {short_id} — {title}")
        print(f"  Beats:  {', '.join(beats)}")
        print(f"  Scenes: {', '.join(scenes)}")

        # Validate beat files
        beat_paths = []
        for b in beats:
            p = narration_dir / f"{b}.mp3"
            if not p.exists():
                print(f"  ERROR: Beat file missing: {p}", file=sys.stderr)
                beat_paths = []
                break
            beat_paths.append(p)

        if not beat_paths:
            results.append({"short_id": short_id, "status": "missing_beats"})
            continue

        # Validate clip files
        clip_paths = []
        for sc in scenes:
            p = video_dir / f"sc_{sc}.mp4"
            if not p.exists():
                print(f"  WARNING: Clip missing: {p} — scene will use black fill")
            clip_paths.append(p if p.exists() else None)

        # Duration
        total_narration = sum(_probe_duration(p) for p in beat_paths)
        print(f"  Narration duration: {total_narration:.2f}s")

        if args.dry_run:
            results.append({"short_id": short_id, "status": "dry_run", "duration": total_narration})
            continue

        # ------------------------------------------------------------------
        # 1. Stitch narration beats for this short
        # ------------------------------------------------------------------
        narr_out = shorts_dir / f"narr_{short_id}.m4a"
        if not _stitch_narration(beat_paths, narr_out):
            print(f"  ERROR: Failed to stitch narration for {short_id}", file=sys.stderr)
            results.append({"short_id": short_id, "status": "narr_stitch_failed"})
            continue
        print(f"  Narration stitched: {narr_out.name}  {_probe_duration(narr_out):.2f}s")

        # ------------------------------------------------------------------
        # 2. Build narration-anchored beat durations
        # ------------------------------------------------------------------
        # Each scene duration = actual duration of its narration beat file.
        beat_durations = [_probe_duration(p) for p in beat_paths]

        # ------------------------------------------------------------------
        # 3. Render short at 1080x1920
        # ------------------------------------------------------------------
        body_path = shorts_dir / f"body_{short_id}.mp4"
        body_ok = _render_short_body(
            clip_paths=clip_paths,
            beat_durations=beat_durations,
            narration_path=narr_out,
            music_path=music_path,
            output_path=body_path,
            short_id=short_id,
            log=log,
        )
        if not body_ok:
            print(f"  ERROR: Body render failed for {short_id}", file=sys.stderr)
            results.append({"short_id": short_id, "status": "render_failed"})
            continue

        # ------------------------------------------------------------------
        # 4. Append end-page
        # ------------------------------------------------------------------
        final_path = episode_dir / f"short_{short_id}_{episode_id}.mp4"
        if end_page_path and end_page_path.exists():
            ep_ok = _append_end_page(body_path, end_page_path, END_PAGE_DURATION, final_path, short_id, log)
            if not ep_ok:
                # Fall back to body-only
                body_path.rename(final_path)
                print(f"  WARNING: End-page append failed; using body only")
            else:
                body_path.unlink(missing_ok=True)
        else:
            body_path.rename(final_path)

        # ------------------------------------------------------------------
        # 5. QA
        # ------------------------------------------------------------------
        qa = run_final_output_qa(
            mp4_path=final_path,
            expected_body_duration=total_narration,
            end_page_duration=END_PAGE_DURATION if (end_page_path and end_page_path.exists()) else 0.0,
            episode_id=f"{episode_id}_{short_id}",
            log=log,
        )
        status = "passed" if qa["passed"] else "failed"
        print(f"  QA {status.upper()}: duration={qa['video_duration_s']:.2f}s  av_mismatch={qa['av_mismatch_s']:.3f}s")
        if qa["blockers"]:
            for b in qa["blockers"]:
                print(f"    BLOCKER: {b}")
        if qa["warnings"]:
            for w in qa["warnings"]:
                print(f"    WARNING: {w}")

        results.append({
            "short_id": short_id,
            "title": title,
            "status": status,
            "file": str(final_path),
            "duration_s": qa["video_duration_s"],
            "qa": qa,
        })
        print(f"  → {final_path.name}  {qa['video_duration_s']:.2f}s")

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"SHORTS GENERATION SUMMARY")
    print(f"{'='*60}")
    for r in results:
        icon = "✓" if r.get("status") == "passed" else ("○" if r.get("status") == "dry_run" else "✗")
        print(f"  {icon} {r['short_id']}: {r.get('status')} "
              f"({r.get('duration_s', r.get('duration', '?')):.1f}s)" if isinstance(r.get('duration_s', r.get('duration')), float)
              else f"  {icon} {r['short_id']}: {r.get('status')}")

    failed = [r for r in results if r.get("status") not in ("passed", "dry_run")]
    if failed:
        print(f"\n{len(failed)} short(s) failed. Check errors above.")
        sys.exit(1)
    else:
        n = len(results)
        print(f"\n{'All ' + str(n) + ' shorts ' + ('planned' if args.dry_run else 'generated')} successfully.")


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


def _find_music_track(assets_root: Path) -> Path | None:
    music_dir = assets_root / "channel" / "music" / "mystery"
    if not music_dir.exists():
        return None
    best: tuple | None = None
    for f in music_dir.glob("*.mp3"):
        if "recovery" in f.stem.lower() or "test" in f.stem.lower():
            continue
        dur = _probe_duration(f)
        if dur < 30.0:
            continue
        if best is None or dur > best[0]:
            best = (dur, f)
    if best is None:
        for f in music_dir.glob("*.mp3"):
            dur = _probe_duration(f)
            if best is None or dur > best[0]:
                best = (dur, f)
    return best[1] if best else None


def _find_end_page(assets_root: Path) -> Path | None:
    for c in [
        assets_root / "end-page.png",
        assets_root / "channel" / "end_page.png",
        assets_root / "channel" / "brand" / "end_page.png",
    ]:
        if c.exists():
            return c
    return None


def _stitch_narration(beat_paths: list[Path], output: Path) -> bool:
    """Concatenate beat MP3 files into one narration track via ffmpeg concat filter."""
    if not beat_paths:
        return False
    if len(beat_paths) == 1:
        # Single beat: re-encode to AAC/m4a
        cmd = [
            "ffmpeg", "-y", "-i", str(beat_paths[0]),
            "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE, str(output),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return r.returncode == 0 and output.exists() and output.stat().st_size > 0
        except Exception:
            return False
    # Build concat filter: N inputs → concat → one output (AAC m4a)
    inputs = []
    for p in beat_paths:
        inputs += ["-i", str(p)]
    filter_str = "".join(f"[{i}:a]" for i in range(len(beat_paths)))
    filter_str += f"concat=n={len(beat_paths)}:v=0:a=1[aout]"
    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + ["-filter_complex", filter_str, "-map", "[aout]",
           "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE, str(output)]
    )
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode == 0 and output.exists() and output.stat().st_size > 0
    except Exception:
        return False


def _render_short_body(
    clip_paths: list[Path | None],
    beat_durations: list[float],
    narration_path: Path,
    music_path: Path,
    output_path: Path,
    short_id: str,
    log,
) -> bool:
    """
    Render the short body (9:16) using ffmpeg.

    Video strategy for each clip:
    - Clips longer than beat: trim.
    - Clips shorter than beat by >1s: setpts slow-motion (factor ≤2) or loop (factor >2).
    - Clips shorter by ≤1s: tpad emergency fallback.
    - Missing clips: lavfi black fill.

    All clips are center-cropped to 1080x1920 (9:16) from 1920x1080 source.
    """
    width, height = SHORT_RESOLUTION.split("x")
    total_duration = sum(beat_durations)

    inputs: list[str] = []
    filter_parts: list[str] = []
    tpad_parts: list[str] = []
    concat_labels: list[str] = []

    clip_idx = 0
    for i, (clip_path, beat_dur) in enumerate(zip(clip_paths, beat_durations)):
        if clip_path and clip_path.exists():
            inputs += ["-i", str(clip_path)]
            clip_dur = _probe_duration(clip_path)

            hold = max(0.0, beat_dur - clip_dur)
            trim_needed = clip_dur > beat_dur + 0.001

            # 9:16 center crop from 16:9 source
            scale_label = f"[sv{clip_idx}]"
            tpad_parts.append(
                f"[{clip_idx}:v]scale={width}:{height}"
                f":force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1{scale_label}"
            )

            if trim_needed:
                trim_label = f"[tv{clip_idx}]"
                tpad_parts.append(
                    f"{scale_label}trim=duration={beat_dur:.3f},"
                    f"setpts=PTS-STARTPTS{trim_label}"
                )
                concat_labels.append(trim_label)
            elif hold > 1.0:
                out_label = f"[pv{clip_idx}]"
                factor = beat_dur / max(clip_dur, 0.001)
                if factor <= 2.0:
                    tpad_parts.append(
                        f"{scale_label}setpts={factor:.6f}*PTS,"
                        f"fps=fps={FRAME_RATE}{out_label}"
                    )
                else:
                    tpad_parts.append(
                        f"{scale_label}loop=loop=-1:size=32767:start=0,"
                        f"trim=duration={beat_dur:.3f},"
                        f"setpts=PTS-STARTPTS{out_label}"
                    )
                concat_labels.append(out_label)
            elif hold > 0.001:
                out_label = f"[pv{clip_idx}]"
                tpad_parts.append(
                    f"{scale_label}tpad=stop_mode=clone:stop_duration={hold:.3f}{out_label}"
                )
                concat_labels.append(out_label)
            else:
                concat_labels.append(scale_label)
            clip_idx += 1
        else:
            # Missing clip: black fill for this beat
            inputs += [
                "-f", "lavfi",
                "-i", f"color=c=black:s={SHORT_RESOLUTION}:r={FRAME_RATE}:d={beat_dur:.3f}",
            ]
            blk_label = f"[bv{clip_idx}]"
            tpad_parts.append(
                f"[{clip_idx}:v]trim=duration={beat_dur:.3f},"
                f"setpts=PTS-STARTPTS{blk_label}"
            )
            concat_labels.append(blk_label)
            clip_idx += 1

    clip_count = clip_idx
    filter_parts.extend(tpad_parts)
    concat_in = "".join(concat_labels)
    filter_parts.append(
        f"{concat_in}concat=n={clip_count}:v=1:a=0[vconcat];"
        f"[vconcat]trim=0:{total_duration:.6f},"
        f"setpts=PTS-STARTPTS[vout]"
    )

    # Audio
    narr_idx = clip_idx
    inputs += ["-i", str(narration_path)]
    music_idx = narr_idx + 1
    inputs += ["-i", str(music_path)]
    filter_parts.append(
        f"[{narr_idx}:a]atrim=0:{total_duration:.6f},asetpts=PTS-STARTPTS,volume=1.0[narr];"
        f"[{music_idx}:a]volume={MUSIC_FACTOR}[msc];"
        f"[narr][msc]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + ["-filter_complex", ";".join(filter_parts)]
        + ["-map", "[vout]", "-map", "[aout]"]
        + [
            "-c:v", VIDEO_CODEC,
            "-c:a", AUDIO_CODEC,
            "-b:a", AUDIO_BITRATE,
            "-crf", CRF,
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-t", f"{total_duration:.6f}",
            str(output_path),
        ]
    )

    log.info("shorts_render_start", short_id=short_id, total_duration=round(total_duration, 3))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        if r.returncode != 0:
            log.error("shorts_render_failed", short_id=short_id, stderr=r.stderr[-1000:])
            return False
    except subprocess.TimeoutExpired:
        log.error("shorts_render_timeout", short_id=short_id)
        return False
    except Exception as exc:
        log.error("shorts_render_error", short_id=short_id, error=str(exc))
        return False

    return output_path.exists() and output_path.stat().st_size > 0


def _append_end_page(body_path: Path, end_page_path: Path, duration: float,
                     output_path: Path, short_id: str, log) -> bool:
    """Append a static end-page PNG card to a short via filter_complex re-encode."""
    width, height = SHORT_RESOLUTION.split("x")
    clip_path = body_path.with_suffix(".endpage.mp4")

    try:
        # Create end-page clip
        ep_cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(FRAME_RATE),
            "-t", f"{duration:.3f}",
            "-i", str(end_page_path),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            (f"[0:v]scale={width}:{height}"
             f":force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black[vout];"
             f"[1:a]volume=0.0[aout]"),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", VIDEO_CODEC, "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
            "-crf", CRF, "-pix_fmt", "yuv420p", "-t", f"{duration:.3f}",
            str(clip_path),
        ]
        r = subprocess.run(ep_cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not clip_path.exists():
            return False

        # Concat body + end-page via filter_complex
        concat_cmd = [
            "ffmpeg", "-y",
            "-i", str(body_path), "-i", str(clip_path),
            "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[vout][aout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", VIDEO_CODEC, "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE,
            "-crf", CRF, "-pix_fmt", "yuv420p",
            str(output_path),
        ]
        r2 = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=300)
        return r2.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        return False
    finally:
        try:
            if clip_path.exists():
                clip_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
