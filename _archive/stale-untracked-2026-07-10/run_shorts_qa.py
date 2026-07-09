#!/usr/bin/env python3
"""
Validate existing shorts for ep_20260629075717_c7abfd.
Also generates missing shorts if needed.
"""

import json
import subprocess
import os
import datetime
import shutil
from pathlib import Path

EP_ID = "ep_20260629075717_c7abfd"
EP_DIR = Path(f"/Users/gawadeditor/hypogeum-brain/.hypogeum-assets/episodes/{EP_ID}")
VIDEO_DIR = EP_DIR / "video"
NARRATION_DIR = EP_DIR / "narration"
SHORTS_SUBDIR = EP_DIR / "shorts"
MUSIC_DIR = Path("/Users/gawadeditor/hypogeum-brain/.hypogeum-assets/channel/music/mystery")
END_PAGE = Path("/Users/gawadeditor/hypogeum-brain/.hypogeum-assets/end-page.png")


def run(cmd, timeout=300):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout, result.stderr, result.returncode


def ffprobe_json(path):
    out, err, rc = run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-show_format", str(path)])
    if rc != 0:
        return None, err
    try:
        return json.loads(out), None
    except:
        return None, out


def get_duration(path):
    data, _ = ffprobe_json(path)
    if not data:
        return None
    fmt = data.get("format", {})
    return float(fmt.get("duration", 0)) or None


def detect_silence(path, noise_db=-50, duration=2.0):
    """Returns list of {start, end, duration} dicts with silence > duration."""
    out, err, _ = run(["ffmpeg", "-i", str(path),
                       "-af", f"silencedetect=n={noise_db}dB:d={duration}",
                       "-f", "null", "-"], timeout=120)
    segments = []
    lines = err.split("\n")
    current_start = None
    for line in lines:
        if "silence_start" in line:
            try:
                current_start = float(line.split("silence_start: ")[1].strip())
            except:
                pass
        elif "silence_end" in line and current_start is not None:
            try:
                parts = line.split("silence_end: ")[1].split(" | ")
                end = float(parts[0].strip())
                dur = float(parts[1].replace("silence_duration: ", "").strip())
                segments.append({"start": round(current_start, 3), "end": round(end, 3), "duration": round(dur, 3)})
                current_start = None
            except:
                pass
    return segments


def detect_freeze(path, freeze_diff=0.003, freeze_dur=10.0):
    """Returns freeze segments > freeze_dur seconds."""
    out, err, _ = run(["ffmpeg", "-i", str(path),
                       "-vf", f"freezedetect=n={freeze_diff}:d={freeze_dur}",
                       "-f", "null", "-"], timeout=120)
    segments = []
    lines = err.split("\n")
    current_start = None
    for line in lines:
        if "freeze_start" in line:
            try:
                current_start = float(line.split("freeze_start: ")[1].strip())
            except:
                pass
        elif "freeze_end" in line and current_start is not None:
            try:
                parts = line.split("freeze_end: ")[1].split(" | ")
                end = float(parts[0].strip())
                dur = float(parts[1].replace("freeze_duration: ", "").strip())
                segments.append({"start": round(current_start, 3), "end": round(end, 3), "duration": round(dur, 3)})
                current_start = None
            except:
                pass
    return segments


def qa_short(path):
    """Returns dict with QA results."""
    result = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        result["passed"] = False
        result["reason"] = "File not found"
        return result

    data, err = ffprobe_json(path)
    if not data:
        result["passed"] = False
        result["reason"] = f"ffprobe failed: {err}"
        return result

    fmt = data.get("format", {})
    dur = float(fmt.get("duration", 0))
    result["duration_s"] = round(dur, 2)

    streams = data.get("streams", [])
    vstream = next((s for s in streams if s["codec_type"] == "video"), None)
    astream = next((s for s in streams if s["codec_type"] == "audio"), None)

    if vstream:
        result["width"] = vstream.get("width")
        result["height"] = vstream.get("height")
        result["resolution"] = f"{vstream.get('width')}x{vstream.get('height')}"
    if astream:
        result["has_audio"] = True
        result["audio_channels"] = astream.get("channels")
    else:
        result["has_audio"] = False

    failures = []
    warnings = []

    # Duration check 45-90s
    if dur < 45:
        failures.append(f"Too short: {dur:.1f}s < 45s")
    elif dur > 90:
        warnings.append(f"Slightly long: {dur:.1f}s > 90s (will check if acceptable)")

    # Resolution check 1080x1920
    if vstream:
        w = vstream.get("width", 0)
        h = vstream.get("height", 0)
        if w != 1080 or h != 1920:
            failures.append(f"Wrong resolution: {w}x{h} (expected 1080x1920)")

    if not astream:
        failures.append("No audio stream")

    result["passed"] = len(failures) == 0
    result["failures"] = failures
    result["warnings"] = warnings
    return result


def get_music_file():
    """Find the mystery music file."""
    if not MUSIC_DIR.exists():
        return None
    files = sorted(MUSIC_DIR.glob("*.mp3"))
    return files[0] if files else None


def stitch_narration(beat_files, out_path):
    """Concatenate narration beat files into a single m4a."""
    concat_list = out_path.parent / f"_concat_{out_path.stem}.txt"
    with open(concat_list, "w") as f:
        for bf in beat_files:
            f.write(f"file '{bf}'\n")
    out, err, rc = run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:a", "aac", "-ar", "44100", "-ac", "1",
        str(out_path)
    ])
    concat_list.unlink(missing_ok=True)
    return rc == 0, err


def build_short(short_num, clip_files, narration_m4a, out_path, music_file):
    """Build a vertical 1080x1920 short using ffmpeg."""
    # Build filter_complex for vertical crops of each clip
    n_clips = len(clip_files)

    # Create filter for each clip: scale to fit 1080 wide, crop to 1920 tall
    # Use setsar=1 to normalize SAR
    filter_parts = []
    for i in range(n_clips):
        # Scale keeping aspect, then pad/crop to 1080x1920
        filter_parts.append(
            f"[{i}:v]scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30[v{i}]"
        )

    # Concatenate all clips
    concat_v = "".join(f"[v{i}]" for i in range(n_clips))
    filter_parts.append(f"{concat_v}concat=n={n_clips}:v=1:a=0[vbody]")

    # Get narration duration to know total video duration
    narr_dur = get_duration(narration_m4a)
    if not narr_dur:
        return False, "Could not get narration duration"

    # Add 5-second end card (black)
    total_dur = narr_dur + 5.0

    # Music: loop [0, 103.1s] using aloop
    music_trim_s = 103.1
    # We'll use aloop to loop the music up to total_dur
    # aloop=loop=-1:size=<samples at 44100>=103.1*44100
    music_loop_samples = int(music_trim_s * 44100)

    # Build input list
    inputs = []
    for cf in clip_files:
        inputs += ["-i", str(cf)]

    # Music input
    inputs += ["-i", str(music_file)]

    # Narration input
    narr_input_idx = n_clips
    music_input_idx = n_clips
    narr_input_idx = n_clips + 1

    # Wait - let's reorganize: clips first, then music, then narration
    music_idx = n_clips
    narr_idx = n_clips + 1

    # Audio filter: mix narration + music bed
    # Music: trim to 103.1s then loop
    audio_filter = (
        f"[{music_idx}:a]aloop=loop=-1:size={music_loop_samples}:start=0,"
        f"atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,volume=0.15[music_bed];"
        f"[{narr_idx}:a]asetpts=PTS-STARTPTS,apad=pad_dur=5[vo_padded];"
        f"[vo_padded][music_bed]amix=inputs=2:duration=longest:weights=1 0.15[aout]"
    )

    # End card: 5 seconds of black at end
    # We'll trim video to narr_dur then add 5s black
    full_filter = ";".join(filter_parts)
    full_filter += f";[vbody]trim=0:{narr_dur:.3f},setpts=PTS-STARTPTS[vbody_trimmed]"
    # Create 5s black
    full_filter += f";color=black:s=1080x1920:r=30:d=5[endcard]"
    full_filter += f";[vbody_trimmed][endcard]concat=n=2:v=1:a=0[vfinal]"
    full_filter += f";{audio_filter}"

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + ["-i", str(narration_m4a)]
        + ["-filter_complex", full_filter]
        + ["-map", "[vfinal]", "-map", "[aout]"]
        + ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
        + ["-c:a", "aac", "-ar", "44100", "-ac", "1"]
        + ["-t", str(total_dur)]
        + [str(out_path)]
    )

    print(f"  Building short {short_num}...")
    out, err, rc = run(cmd, timeout=300)
    if rc != 0:
        return False, err[-2000:]
    return True, None


def main():
    print("=== SHORTS QA AND GENERATION ===\n")

    SHORTS_SUBDIR.mkdir(exist_ok=True)

    # Read script/visual package to understand episode structure
    script_pkg_path = EP_DIR / f"script_package_{EP_ID}.json"
    narr_pkg_path = EP_DIR / f"narration_package_{EP_ID}.json"
    visual_pkg_path = EP_DIR / f"visual_package_{EP_ID}.json"

    script_pkg = json.loads(script_pkg_path.read_text()) if script_pkg_path.exists() else {}
    narr_pkg = json.loads(narr_pkg_path.read_text()) if narr_pkg_path.exists() else {}
    visual_pkg = json.loads(visual_pkg_path.read_text()) if visual_pkg_path.exists() else {}

    # Get music
    music_file = get_music_file()
    print(f"Music file: {music_file}")

    # Define 5 shorts with their clip and beat assignments
    # Each short covers a distinct narrative arc of the Hypogeum story
    SHORT_DESIGNS = [
        {
            "num": 1,
            "theme": "The Secret Underground - Hook",
            "clips": ["sc_s01.mp4", "sc_s02.mp4", "sc_s03.mp4", "sc_s04.mp4", "sc_s05.mp4"],
            "beats": list(range(1, 6)),  # beats 1-5
        },
        {
            "num": 2,
            "theme": "Engineering the Hypogeum",
            "clips": ["sc_s07.mp4", "sc_s08.mp4", "sc_s09.mp4", "sc_s10.mp4", "sc_s11.mp4", "sc_s12.mp4"],
            "beats": list(range(7, 13)),
        },
        {
            "num": 3,
            "theme": "The Spectacle of Death",
            "clips": ["sc_s13.mp4", "sc_s14.mp4", "sc_s15.mp4", "sc_s16.mp4", "sc_s17.mp4"],
            "beats": list(range(13, 18)),
        },
        {
            "num": 4,
            "theme": "The Forgotten Workforce",
            "clips": ["sc_s19.mp4", "sc_s20.mp4", "sc_s21.mp4", "sc_s22.mp4", "sc_s23.mp4", "sc_s24.mp4"],
            "beats": list(range(19, 25)),
        },
        {
            "num": 5,
            "theme": "The Mystery Endures",
            "clips": ["sc_s28.mp4", "sc_s29.mp4", "sc_s30.mp4", "sc_s31.mp4", "sc_s32.mp4", "sc_s33.mp4"],
            "beats": list(range(28, 34)),
        },
    ]

    # Check for existing shorts (in root episode dir with old naming)
    existing_shorts = {
        1: EP_DIR / f"short_sh_001_{EP_ID}.mp4",
        2: EP_DIR / f"short_sh_002_{EP_ID}.mp4",
        3: EP_DIR / f"short_sh_003_{EP_ID}.mp4",
        4: EP_DIR / f"short_sh_004_{EP_ID}.mp4",
        5: EP_DIR / f"short_sh_005_{EP_ID}.mp4",
    }

    # Target paths in shorts/ subdir
    target_shorts = {
        i: SHORTS_SUBDIR / f"short_{i}_{EP_ID}.mp4"
        for i in range(1, 6)
    }

    shorts_qa = []

    for design in SHORT_DESIGNS:
        n = design["num"]
        target = target_shorts[n]
        existing = existing_shorts[n]

        print(f"\n--- Short {n}: {design['theme']} ---")

        # Check if target already exists and valid
        if target.exists():
            qa = qa_short(target)
            if qa["passed"]:
                print(f"  Target already exists and valid: {target}")
                qa["source"] = "existing_target"
                shorts_qa.append(qa)
                continue

        # Check if existing (old naming) is valid
        if existing.exists():
            print(f"  Checking existing short: {existing}")
            qa = qa_short(existing)
            print(f"  QA result: passed={qa['passed']}, dur={qa.get('duration_s','?')}, res={qa.get('resolution','?')}")
            if qa["passed"]:
                # Copy to target
                print(f"  Copying to target: {target}")
                shutil.copy2(existing, target)
                qa["source"] = "existing_copied"
                qa["path"] = str(target)
                shorts_qa.append(qa)
                continue
            else:
                print(f"  Existing short FAILED QA: {qa['failures']}")
        else:
            print(f"  No existing short found, building from scratch")

        # Build short from scratch
        if not music_file:
            print(f"  ERROR: No music file found, cannot build short")
            shorts_qa.append({"path": str(target), "passed": False, "reason": "No music file"})
            continue

        # Stitch narration
        beat_files = [NARRATION_DIR / f"b_{b:03d}.mp3" for b in design["beats"]]
        missing_beats = [str(f) for f in beat_files if not f.exists()]
        if missing_beats:
            print(f"  ERROR: Missing beat files: {missing_beats}")
            shorts_qa.append({"path": str(target), "passed": False, "reason": f"Missing beats: {missing_beats}"})
            continue

        narr_out = SHORTS_SUBDIR / f"narr_short_{n}_{EP_ID}.m4a"
        print(f"  Stitching narration from beats: {design['beats']}")
        ok, err = stitch_narration(beat_files, narr_out)
        if not ok:
            print(f"  ERROR stitching narration: {err}")
            shorts_qa.append({"path": str(target), "passed": False, "reason": f"Narration stitch failed: {err}"})
            continue

        narr_dur = get_duration(narr_out)
        print(f"  Narration duration: {narr_dur:.1f}s")

        # Check clip files
        clip_files = [VIDEO_DIR / c for c in design["clips"]]
        missing_clips = [str(f) for f in clip_files if not f.exists()]
        if missing_clips:
            print(f"  ERROR: Missing clips: {missing_clips}")
            shorts_qa.append({"path": str(target), "passed": False, "reason": f"Missing clips: {missing_clips}"})
            continue

        # Build short
        ok, err = build_short(n, clip_files, narr_out, target, music_file)
        if not ok:
            print(f"  ERROR building short: {err}")
            shorts_qa.append({"path": str(target), "passed": False, "reason": f"ffmpeg failed: {err}"})
            continue

        # QA the built short
        qa = qa_short(target)
        qa["source"] = "generated"
        qa["theme"] = design["theme"]
        print(f"  QA: passed={qa['passed']}, dur={qa.get('duration_s','?')}, res={qa.get('resolution','?')}")
        if not qa["passed"]:
            print(f"  Failures: {qa['failures']}")
        shorts_qa.append(qa)

    # Write QA report
    report = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "episode_id": EP_ID,
        "shorts": shorts_qa,
        "all_passed": all(s.get("passed", False) for s in shorts_qa),
        "pass_count": sum(1 for s in shorts_qa if s.get("passed", False)),
        "fail_count": sum(1 for s in shorts_qa if not s.get("passed", False)),
    }

    qa_path = EP_DIR / f"shorts_qa_report_{EP_ID}.json"
    with open(qa_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nShorts QA report written to: {qa_path}")

    print(f"\n=== SHORTS SUMMARY ===")
    print(f"Total: {len(shorts_qa)}, Passed: {report['pass_count']}, Failed: {report['fail_count']}")
    for s in shorts_qa:
        status = "PASS" if s.get("passed") else "FAIL"
        print(f"  [{status}] {Path(s['path']).name}: dur={s.get('duration_s','?')}s, res={s.get('resolution','?')}")

    return report


if __name__ == "__main__":
    main()
