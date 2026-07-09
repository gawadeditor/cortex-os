#!/usr/bin/env python3
"""
Full audit script for ep_20260629075717_c7abfd.
Runs ffprobe/ffmpeg detections and writes full_audit_report JSON.
"""

import json
import subprocess
import os
import datetime
from pathlib import Path

EP_ID = "ep_20260629075717_c7abfd"
EP_DIR = Path(f"/Users/gawadeditor/hypogeum-brain/.hypogeum-assets/episodes/{EP_ID}")
MP4 = EP_DIR / f"main_{EP_ID}.mp4"
VIDEO_DIR = EP_DIR / "video"
NARRATION_DIR = EP_DIR / "narration"
THUMBS_DIR = EP_DIR / "thumbnails"
VISUAL_PACKAGE = EP_DIR / f"visual_package_{EP_ID}.json"


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


def detect_silence(path, noise_db=-50, duration=0.5):
    """Returns list of {start, end, duration} dicts."""
    out, err, _ = run(["ffmpeg", "-i", str(path),
                       "-af", f"silencedetect=n={noise_db}dB:d={duration}",
                       "-f", "null", "-"], timeout=300)
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


def detect_freeze(path, freeze_diff=0.003, freeze_dur=2.0):
    """Returns list of {start, end, duration} freeze segments."""
    out, err, _ = run(["ffmpeg", "-i", str(path),
                       "-vf", f"freezedetect=n={freeze_diff}:d={freeze_dur}",
                       "-f", "null", "-"], timeout=300)
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


def detect_black(path):
    """Returns list of {start, end, duration} black frame segments."""
    out, err, _ = run(["ffmpeg", "-i", str(path),
                       "-vf", "blackdetect=d=0.5:pic_th=0.9",
                       "-f", "null", "-"], timeout=300)
    segments = []
    lines = err.split("\n")
    for line in lines:
        if "black_start" in line:
            try:
                parts = {}
                for item in line.split() :
                    if ":" in item:
                        k, v = item.split(":", 1)
                        parts[k] = v
                start = float(parts.get("black_start", 0))
                end = float(parts.get("black_end", 0))
                dur = float(parts.get("black_duration", 0))
                segments.append({"start": round(start, 3), "end": round(end, 3), "duration": round(dur, 3)})
            except:
                pass
    return segments


def check_vo_completeness():
    result = {"expected_beats": 33, "found_beats": 0, "missing": [], "zero_dur": [], "beat_durations": {}}
    for i in range(1, 34):
        fname = NARRATION_DIR / f"b_{i:03d}.mp3"
        if not fname.exists():
            result["missing"].append(f"b_{i:03d}.mp3")
        else:
            dur = get_duration(fname)
            result["beat_durations"][f"b_{i:03d}"] = round(dur, 3) if dur else 0
            if not dur or dur < 0.1:
                result["zero_dur"].append(f"b_{i:03d}.mp3")
            else:
                result["found_beats"] += 1
    result["vo_complete"] = (len(result["missing"]) == 0 and len(result["zero_dur"]) == 0)
    return result


def check_thumbnail():
    result = {"exists": False, "valid": False, "size_bytes": 0, "dimensions": None, "path": str(THUMBS_DIR / "main.jpg")}
    main_thumb = THUMBS_DIR / "main.jpg"
    if main_thumb.exists():
        result["exists"] = True
        result["size_bytes"] = main_thumb.stat().st_size
        if result["size_bytes"] > 10000:
            data, _ = ffprobe_json(main_thumb)
            if data:
                for s in data.get("streams", []):
                    if s.get("codec_type") == "video":
                        result["dimensions"] = f"{s['width']}x{s['height']}"
                        result["valid"] = True
    return result


def check_visual_package_duplicates():
    """Scan visual_package for duplicate scene_ids or same asset used within 30s."""
    if not VISUAL_PACKAGE.exists():
        return {"error": "visual_package not found"}
    with open(VISUAL_PACKAGE) as f:
        vp = json.load(f)

    scenes = vp.get("scenes", [])
    scene_ids = []
    asset_ids = []
    dup_scene_ids = []
    dup_asset_close = []

    seen_scene_ids = {}
    for sc in scenes:
        sid = sc.get("scene_id") or sc.get("id")
        aid = sc.get("asset_id") or sc.get("clip_asset_id")
        t_start = sc.get("timeline_start", 0)

        if sid:
            if sid in seen_scene_ids:
                dup_scene_ids.append({"scene_id": sid, "first_at": seen_scene_ids[sid], "repeat_at": t_start})
            seen_scene_ids[sid] = t_start

        if aid:
            # Check if this asset was used recently (< 30s gap)
            recent = [(prev_t, prev_sid) for prev_t, prev_sid in asset_ids if abs(t_start - prev_t) < 30]
            if recent:
                for pt, ps in recent:
                    dup_asset_close.append({
                        "asset_id": aid,
                        "scene_id": sid,
                        "prev_scene_id": ps,
                        "gap_s": round(abs(t_start - pt), 2)
                    })
            asset_ids.append((t_start, sid))

    return {
        "total_scenes": len(scenes),
        "duplicate_scene_ids": dup_scene_ids,
        "duplicate_asset_within_30s": dup_asset_close,
        "has_duplicates": len(dup_scene_ids) > 0 or len(dup_asset_close) > 0
    }


def check_video_clips():
    clips = {}
    for f in sorted(VIDEO_DIR.glob("sc_s*.mp4")):
        if any(suffix in f.name for suffix in ["_pass1", "_pass2", "_pass3", "_repair", "_combined", "_original", "_repair2", "_repair3"]):
            continue  # skip intermediate files
        dur = get_duration(f)
        clips[f.name] = round(dur, 3) if dur else 0
    return clips


def main():
    print(f"Starting full audit of {MP4}")
    print(f"File size: {MP4.stat().st_size / 1e6:.1f} MB")

    # Stream info
    print("\n--- Stream Info ---")
    probe_data, err = ffprobe_json(MP4)
    streams_info = []
    video_duration = None
    audio_duration = None

    if probe_data:
        for s in probe_data.get("streams", []):
            info = {
                "index": s["index"],
                "codec_type": s["codec_type"],
                "codec_name": s["codec_name"],
                "duration": float(s.get("duration", 0)) or None
            }
            if s["codec_type"] == "video":
                info["width"] = s.get("width")
                info["height"] = s.get("height")
                info["fps"] = s.get("r_frame_rate")
                video_duration = info["duration"]
            elif s["codec_type"] == "audio":
                info["sample_rate"] = s.get("sample_rate")
                info["channels"] = s.get("channels")
                audio_duration = info["duration"]
            streams_info.append(info)
            print(f"  {info}")

    av_mismatch = abs(video_duration - audio_duration) if video_duration and audio_duration else None
    print(f"  AV mismatch: {av_mismatch:.3f}s" if av_mismatch is not None else "  AV mismatch: N/A")

    # Silence detection
    print("\n--- Silence Detection (-50dB) ---")
    silence_segs = detect_silence(MP4)
    print(f"  Found {len(silence_segs)} silence segments")
    for s in silence_segs:
        print(f"    {s}")

    # Freeze detection
    print("\n--- Freeze Detection ---")
    freeze_segs = detect_freeze(MP4)
    print(f"  Found {len(freeze_segs)} freeze segments")
    body_freezes = [s for s in freeze_segs if s["end"] < (video_duration - 20)] if video_duration else []
    end_freezes = [s for s in freeze_segs if s["start"] >= (video_duration - 25)] if video_duration else []
    for s in freeze_segs:
        label = "[END-PAGE]" if s["start"] >= (video_duration - 25) else "[BODY]"
        print(f"    {label} {s}")

    # Black frame detection
    print("\n--- Black Frame Detection ---")
    black_segs = detect_black(MP4)
    print(f"  Found {len(black_segs)} black segments")
    for s in black_segs[:10]:
        print(f"    {s}")

    # End page timing
    end_page_start = None
    if freeze_segs:
        last_long_freeze = None
        for s in freeze_segs:
            if s["duration"] >= 5 and s["start"] >= (video_duration - 30):
                last_long_freeze = s
        if last_long_freeze:
            end_page_start = last_long_freeze["start"]
    print(f"\n--- End Page ---")
    print(f"  Estimated end_page_start: {end_page_start}")

    # VO completeness
    print("\n--- VO Completeness ---")
    vo_result = check_vo_completeness()
    print(f"  Beats found: {vo_result['found_beats']}/33")
    print(f"  Missing: {vo_result['missing']}")
    print(f"  Zero-duration: {vo_result['zero_dur']}")
    print(f"  VO complete: {vo_result['vo_complete']}")

    # Thumbnail
    print("\n--- Thumbnail ---")
    thumb_result = check_thumbnail()
    print(f"  Exists: {thumb_result['exists']}, Valid: {thumb_result['valid']}")
    print(f"  Size: {thumb_result['size_bytes']/1024:.1f} KB, Dimensions: {thumb_result['dimensions']}")

    # Visual package duplicates
    print("\n--- Visual Package Duplicate Check ---")
    dup_result = check_visual_package_duplicates()
    print(f"  Total scenes: {dup_result.get('total_scenes', 'N/A')}")
    print(f"  Duplicate scene IDs: {len(dup_result.get('duplicate_scene_ids', []))}")
    print(f"  Duplicate assets within 30s: {len(dup_result.get('duplicate_asset_within_30s', []))}")

    # Determine blockers
    blockers = []
    warnings = []

    for s in body_freezes:
        if s["duration"] > 30:
            blockers.append(f"Long freeze in body: {s['start']:.1f}s-{s['end']:.1f}s ({s['duration']:.1f}s)")
        elif s["duration"] > 2:
            warnings.append(f"Short freeze in body: {s['start']:.1f}s-{s['end']:.1f}s ({s['duration']:.1f}s)")

    if silence_segs:
        for s in silence_segs:
            if s["duration"] > 2 and s["end"] < (video_duration - 20 if video_duration else 9999):
                blockers.append(f"Silence in body: {s['start']:.1f}s ({s['duration']:.1f}s)")

    if not vo_result["vo_complete"]:
        blockers.append(f"VO incomplete: missing={vo_result['missing']}, zero_dur={vo_result['zero_dur']}")

    if av_mismatch and av_mismatch > 1.0:
        blockers.append(f"AV mismatch: {av_mismatch:.3f}s")

    print(f"\n--- Summary ---")
    print(f"  Blockers: {blockers}")
    print(f"  Warnings: {warnings}")
    print(f"  QA PASSED: {len(blockers) == 0}")

    # Build and write report
    report = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "episode_id": EP_ID,
        "mp4_path": str(MP4),
        "file_size_bytes": MP4.stat().st_size,
        "streams": streams_info,
        "video_duration_s": round(video_duration, 3) if video_duration else None,
        "audio_duration_s": round(audio_duration, 3) if audio_duration else None,
        "av_mismatch_s": round(av_mismatch, 3) if av_mismatch is not None else None,
        "silence_segments": silence_segs,
        "freeze_segments": freeze_segs,
        "body_freeze_segments": body_freezes,
        "end_page_freezes": end_freezes,
        "black_segments": black_segs,
        "end_page_start_s": round(end_page_start, 3) if end_page_start else None,
        "vo_completeness": vo_result,
        "thumbnail": thumb_result,
        "visual_package_duplicates": dup_result,
        "blockers": blockers,
        "warnings": warnings,
        "qa_passed": len(blockers) == 0
    }

    out_path = EP_DIR / f"full_audit_report_{EP_ID}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nAudit report written to: {out_path}")
    return report


if __name__ == "__main__":
    main()
