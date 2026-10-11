"""
hypogeum/retention_moments.py — tie where viewers LEFT to what was actually on screen.

Platform numbers say "38% were gone by 25%". This module says what the viewer heard and saw at that second:

    timeline_for_episode(ep_dir)          -> [beat dicts: start/end seconds, spoken text, scene visual, signals]
    exit_events(curve, duration_s)        -> [{t, loss, ...}] the biggest drops (and replay peaks) of a retention curve
    moments_for_video(curve, duration_s, timeline) -> exit events annotated with the beat/scene at that second
    aggregate(moments_by_video)           -> cross-video pattern (by beat role, scene motion, position, text traits)

Pure functions over files already on disk (script_package, narration_package, visual_package) and the YouTube
retention curve already stored in platform_analytics.retention_curve_json. No network, no DB writes.

Timing model: narration_segments are played back to back in order, so a beat starts where the previous one ended
(cumulative `duration_seconds`). Each scene is bound to a beat through `narration_reference`. It is an approximation
(assembly adds small gaps and the outro may be padded) and every moment says so via `timing_confidence`.
"""
from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# episode timeline
# ---------------------------------------------------------------------------

def find_episode_dir(episode_id: str, roots: List[Path]) -> Optional[Path]:
    for root in roots:
        for p in sorted(glob.glob(str(Path(root) / f"{episode_id}*"))):
            if Path(p).is_dir() and list(Path(p).glob("narration_package_*.json")):
                return Path(p)
    return None


def _load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _beat_texts(script: dict) -> Dict[str, str]:
    """{beat_id: spoken text} from anywhere in a script_package (sections, shorts_scripts, flat beats)."""
    out: Dict[str, str] = {}

    def walk(o):
        if isinstance(o, dict):
            if "beat_id" in o and isinstance(o.get("text"), str):
                out.setdefault(o["beat_id"], o["text"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(script)
    return out


def timeline_for_episode(ep_dir: Path, *, short_id: Optional[str] = None) -> List[dict]:
    """Beats in playback order with start/end seconds, spoken text, bound scene and its visual spec."""
    ep_dir = Path(ep_dir)
    narr = _load(next(iter(sorted(ep_dir.glob("narration_package_*.json"))), ep_dir / "x"))
    script = _load(next(iter(sorted(ep_dir.glob("script_package_*.json"))), ep_dir / "x"))
    visual = _load(next(iter(sorted(ep_dir.glob("visual_package_*.json"))), ep_dir / "x"))
    texts = _beat_texts(script)
    scenes_by_beat: Dict[str, dict] = {}
    for sc in visual.get("scenes") or []:
        if short_id and sc.get("short_id") not in (None, short_id):
            continue
        scenes_by_beat.setdefault(sc.get("narration_reference"), sc)

    t = 0.0
    out: List[dict] = []
    for seg in narr.get("narration_segments") or []:
        dur = float(seg.get("duration_seconds") or 0.0)
        beat = seg.get("beat_reference")
        sc = scenes_by_beat.get(beat) or {}
        vs = sc.get("visual_spec") or {}
        out.append({
            "beat_id": beat,
            "start": round(t, 2), "end": round(t + dur, 2),
            "text": texts.get(beat, ""),
            "scene_id": sc.get("scene_id"),
            "signals": sc.get("narrative_signals") or [],
            "subject": vs.get("subject"), "framing": vs.get("framing"),
            "motion_type": vs.get("motion_type"), "mood": vs.get("mood"),
        })
        t += dur
    return out


def beat_at(timeline: List[dict], t: float) -> Optional[dict]:
    for b in timeline:
        if b["start"] <= t < b["end"]:
            return b
    return timeline[-1] if timeline and t >= timeline[-1]["end"] else None


# ---------------------------------------------------------------------------
# retention curve
# ---------------------------------------------------------------------------

def _points(curve: list) -> List[tuple]:
    pts = []
    for p in curve or []:
        x = p.get("elapsedVideoTimeRatio", p.get("elapsedRatio"))
        y = p.get("audienceWatchRatio", p.get("watchRatio"))
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            pts.append((float(x), float(y)))
    return sorted(pts)


def exit_events(curve: list, duration_s: float, *, top: int = 3, min_loss: float = 0.04, window: int = 3) -> List[dict]:
    """The sharpest drops (viewers leaving) and the strongest rises (re-watching) of a retention curve.

    `loss` is the drop in audienceWatchRatio across `window` curve steps (≈ window% of the video), in units of
    "share of the starting audience" — so 0.12 means 12 points of the initial viewers gone in that stretch."""
    pts = _points(curve)
    if len(pts) <= window or duration_s <= 0:
        return []
    deltas = []
    for i in range(len(pts) - window):
        x0, y0 = pts[i]
        x1, y1 = pts[i + window]
        deltas.append((y0 - y1, x0, x1, y0, y1))
    exits: List[dict] = []
    for loss, x0, x1, y0, y1 in sorted(deltas, reverse=True):
        if loss < min_loss:
            break
        # skip overlapping windows so one cliff is reported once
        if any(not (x1 <= e["x0"] or x0 >= e["x1"]) for e in exits):
            continue
        exits.append({"kind": "exit", "loss": round(loss, 3), "x0": x0, "x1": x1,
                      "t0": round(x0 * duration_s, 1), "t1": round(x1 * duration_s, 1),
                      "t": round((x0 + x1) / 2 * duration_s, 1), "ratio_before": round(y0, 2), "ratio_after": round(y1, 2)})
        if len(exits) >= top:
            break
    rises = []
    for i in range(len(pts) - window):
        x0, y0 = pts[i]
        x1, y1 = pts[i + window]
        if y1 - y0 >= min_loss:
            rises.append((y1 - y0, x0, x1, y0, y1))
    for gain, x0, x1, y0, y1 in sorted(rises, reverse=True)[:1]:
        exits.append({"kind": "rewatch", "loss": round(-gain, 3), "x0": x0, "x1": x1,
                      "t0": round(x0 * duration_s, 1), "t1": round(x1 * duration_s, 1),
                      "t": round((x0 + x1) / 2 * duration_s, 1), "ratio_before": round(y0, 2), "ratio_after": round(y1, 2)})
    return exits


def moments_for_video(curve: list, duration_s: float, timeline: List[dict], *, timing_confidence: str = "approx") -> List[dict]:
    """Each exit/rewatch event annotated with the beat that was playing between its t0 and t1."""
    out = []
    for ev in exit_events(curve, duration_s):
        b = beat_at(timeline, ev["t"]) or {}
        out.append({**ev, "timing_confidence": timing_confidence, "beat_id": b.get("beat_id"), "scene_id": b.get("scene_id"),
                    "text": b.get("text", ""), "signals": b.get("signals", []), "subject": b.get("subject"),
                    "framing": b.get("framing"), "motion_type": b.get("motion_type"), "mood": b.get("mood"),
                    "beat_start": b.get("start"), "beat_end": b.get("end"),
                    "beat_index": next((i for i, x in enumerate(timeline) if x.get("beat_id") == b.get("beat_id")), None),
                    "beats_total": len(timeline)})
    return out


# ---------------------------------------------------------------------------
# traits + aggregation
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d")


def text_traits(text: str) -> dict:
    words = re.findall(r"[\w'’-]+", text or "")
    return {"words": len(words), "has_number": bool(_NUMBER.search(text or "")),
            "is_question": (text or "").strip().endswith("?"), "long_beat": len(words) >= 28}


def aggregate(moments_by_video: Dict[str, List[dict]]) -> dict:
    """Cross-video pattern: where in the video, which beat role, which scene motion and what kind of line the
    biggest exit sits on (only 'exit' events, the single sharpest per video, and every exit weighted by loss)."""
    rows = []
    for vid, ms in moments_by_video.items():
        exits = [m for m in ms if m["kind"] == "exit"]
        if exits:
            rows.append((vid, max(exits, key=lambda m: m["loss"])))
    n = len(rows)
    if not n:
        return {"videos": 0}

    def share(pred):
        return round(sum(1 for _, m in rows if pred(m)) / n, 2)

    from collections import Counter
    role = Counter(); motion = Counter(); framing = Counter()
    for _, m in rows:
        role.update(m.get("signals") or ["(none)"])
        motion.update([m.get("motion_type") or "(none)"])
        framing.update([m.get("framing") or "(none)"])
    pos = [m["x0"] for _, m in rows]
    return {
        "videos": n,
        "mean_position_of_worst_exit": round(sum(pos) / n, 2),
        "worst_exit_in_first_quarter": share(lambda m: m["x0"] < 0.25),
        "worst_exit_on_first_beat": share(lambda m: m.get("beat_index") == 0),
        "worst_exit_on_long_line": share(lambda m: text_traits(m.get("text", ""))["long_beat"]),
        "worst_exit_on_line_with_number": share(lambda m: text_traits(m.get("text", ""))["has_number"]),
        "worst_exit_signals": role.most_common(6),
        "worst_exit_motion_types": motion.most_common(5),
        "worst_exit_framings": framing.most_common(5),
        "mean_loss_at_worst_exit": round(sum(m["loss"] for _, m in rows) / n, 3),
    }


# ---------------------------------------------------------------------------
# per-beat loss profile
# ---------------------------------------------------------------------------

def beat_losses(curve: list, duration_s: float, timeline: List[dict]) -> List[dict]:
    """For every beat: how much of the starting audience was lost while that beat played
    (ratio at the beat's start minus ratio at its end). Positive = viewers left."""
    pts = _points(curve)
    if not pts or duration_s <= 0 or not timeline:
        return []

    def ratio_at(t: float) -> float:
        x = min(max(t / duration_s, 0.0), 1.0)
        return min(pts, key=lambda p: abs(p[0] - x))[1]

    out = []
    for i, b in enumerate(timeline):
        out.append({"beat_index": i, "beat_id": b.get("beat_id"), "start": b["start"], "end": b["end"],
                    "loss": round(ratio_at(b["start"]) - ratio_at(min(b["end"], duration_s)), 3),
                    "text": b.get("text", ""), "signals": b.get("signals", []), "subject": b.get("subject"),
                    "framing": b.get("framing"), "motion_type": b.get("motion_type"), "mood": b.get("mood"),
                    "scene_id": b.get("scene_id")})
    return out
