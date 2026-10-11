"""
hypogeum/moment_learning.py — the factory learns, by itself, WHERE viewers leave and WHAT was on screen then.

Runs inside the worker's learning cycle (run_worker._run_learning_cycle), per channel:

    1. every YouTube video with a real retention curve is matched to its episode folder and its narration timeline
       (hypogeum.retention_moments),
    2. the sharpest exits / re-watches and the loss of every beat are stored in `retention_moments`, with the spoken
       line, the planned scene and (when the render is reachable) the ACTUAL frame at that second,
    3. cross-video patterns are derived from the table (which beat position loses the most, what was said and shown
       there, and what a beat that HELD viewers looked like),
    4. when the evidence is strong enough a channel-scoped directive (dimension "retention_moments") is written
       with those real examples; the Narrative Architect and Script Engine read it on the next episode.

Evidence gates: at least MIN_VIDEOS videos, and the pattern must be material (see _material). Below that nothing is
written — a pattern from three videos is a story, not a finding. Never raises, never spends money, no network.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from hypogeum.logger import get_logger
from hypogeum.retention_moments import (
    beat_losses, exit_events, find_episode_dir, moments_for_video, timeline_for_episode,
)

log = get_logger(agent="moment_learning")

MIN_VIDEOS = 6
DIRECTIVE_DIMENSION = "retention_moments"
_ROOT = Path(__file__).resolve().parent.parent
FRAME_DIR = _ROOT / ".hypogeum-assets" / "retention_frames"
_NAS = Path("/Volumes/Archive/Cortex")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _roots(channel_id: str) -> List[Path]:
    return [_NAS / channel_id, _ROOT / ".hypogeum-assets" / "episodes"]


def _youtube_videos(conn, channel_id: str) -> List[dict]:
    rows = conn.execute(
        "SELECT pa.platform_video_id vid, pa.episode_id ep, MAX(pa.views) views, MAX(pa.avg_view_pct) vp, "
        "MAX(pa.avg_watch_time_s) aw, "
        "(SELECT q.retention_curve_json FROM platform_analytics q WHERE q.platform_video_id=pa.platform_video_id "
        " AND length(q.retention_curve_json)>5 LIMIT 1) cur "
        "FROM platform_analytics pa WHERE pa.platform='youtube' AND pa.source='api' AND pa.platform_video_id IS NOT NULL "
        "AND (SELECT et.channel_id FROM episode_topics et WHERE et.episode_id=pa.episode_id LIMIT 1)=? "
        "GROUP BY pa.platform_video_id", (channel_id,)).fetchall()
    return [dict(r) for r in rows if r["cur"] and r["vp"] and r["aw"]]


def extract_frame(video_path: Path, t: float, out_path: Path) -> Optional[str]:
    """One JPEG frame at second `t`. None when the render is unreachable or ffmpeg fails."""
    try:
        if not Path(video_path).exists():
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(video_path),
                            "-frames:v", "1", "-q:v", "3", str(out_path)], capture_output=True, timeout=30)
        return str(out_path) if r.returncode == 0 and out_path.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _render_path(ep_dir: Path, episode_id: str) -> Optional[Path]:
    for name in (f"main_{episode_id}.mp4",):
        p = ep_dir / name
        if p.exists():
            return p
    hits = sorted(ep_dir.glob("main*.mp4"))
    return hits[0] if hits else None


def _cache_timeline(conn, episode_id: str, tl: List[dict]) -> None:
    try:
        conn.execute("INSERT OR REPLACE INTO episode_timelines (episode_id, timeline_json, total_seconds, cached_at) VALUES (?,?,?,?)",
                     (episode_id, json.dumps(tl, ensure_ascii=False), tl[-1]["end"], _now()))
    except Exception:  # noqa: BLE001 -- the cache is an optimisation, never a reason to fail
        pass


def _cached_timeline(conn, episode_id: str) -> Optional[List[dict]]:
    try:
        row = conn.execute("SELECT timeline_json FROM episode_timelines WHERE episode_id = ?", (episode_id,)).fetchone()
        return json.loads(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None


def analyse_channel(conn, channel_id: str, *, frames: bool = True) -> dict:
    """Pure read of the DB + episode folders. Returns {"videos": {vid: {...}}, "skipped": [...]}."""
    out: Dict[str, dict] = {}
    skipped: List[tuple] = []
    for v in _youtube_videos(conn, channel_id):
        dur = v["aw"] / v["vp"]
        d = find_episode_dir(v["ep"], _roots(channel_id))
        rp = None
        if d is not None:
            tl = timeline_for_episode(d)
            if tl:
                _cache_timeline(conn, v["ep"], tl)
            rp = _render_path(d, v["ep"])
        else:
            # folder unreachable (archive/NAS down, or never kept): use the cached timeline
            tl = _cached_timeline(conn, v["ep"])
            if not tl:
                skipped.append((v["vid"], "episode folder not found and no cached timeline"))
                continue
        total = tl[-1]["end"] if tl else 0
        if not tl or abs(total - dur) > 6:
            skipped.append((v["vid"], f"video {dur:.0f}s vs narration {total:.0f}s"))
            continue
        curve = json.loads(v["cur"])
        ms = moments_for_video(curve, dur, tl)
        if frames and rp:
            for i, m in enumerate(ms):
                if m["kind"] == "exit" and i < 2:
                    m["frame_path"] = extract_frame(rp, m["t"], FRAME_DIR / f"{v['vid']}_{m['kind']}_{int(m['t'] * 10)}.jpg")
        out[v["vid"]] = {"episode_id": v["ep"], "views": v["views"], "avg_view_pct": v["vp"], "duration_s": dur,
                         "moments": ms, "beats": beat_losses(curve, dur, tl)}
    return {"videos": out, "skipped": skipped}


def store(conn, channel_id: str, analysis: dict) -> int:
    n = 0
    for vid, info in analysis["videos"].items():
        conn.execute("DELETE FROM retention_moments WHERE video_id = ?", (vid,))
        for i, m in enumerate(info["moments"]):
            conn.execute(
                "INSERT INTO retention_moments (moment_id, channel_id, episode_id, video_id, kind, t0, t1, loss, video_views, "
                "beat_id, beat_index, beats_total, signals_json, spoken_text, scene_id, visual_json, frame_path, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{vid}:{m['kind']}:{i}", channel_id, info["episode_id"], vid, m["kind"], m["t0"], m["t1"], m["loss"],
                 info["views"], m.get("beat_id"), m.get("beat_index"), m.get("beats_total"),
                 json.dumps(m.get("signals") or []), m.get("text"), m.get("scene_id"),
                 json.dumps({k: m.get(k) for k in ("subject", "framing", "motion_type", "mood")}, ensure_ascii=False),
                 m.get("frame_path"), _now()))
            n += 1
    return n


# ---------------------------------------------------------------------------
# pattern -> directive
# ---------------------------------------------------------------------------

def _words(t: str) -> int:
    return len((t or "").split())


def derive_pattern(analysis: dict) -> Optional[dict]:
    """The beat position that loses the most viewers across videos, with real examples of a beat that lost them and
    of the SAME position holding them. None when there is not enough material evidence."""
    vids = analysis["videos"]
    if len(vids) < MIN_VIDEOS:
        return None
    by_pos: Dict[int, List[dict]] = {}
    for info in vids.values():
        beats = info["beats"]
        for b in beats[:-1]:                       # the last beat is the outro: ending is not "leaving"
            by_pos.setdefault(b["beat_index"], []).append({**b, "views": info["views"], "vp": info["avg_view_pct"]})
    means = {k: statistics.mean(x["loss"] for x in v) for k, v in by_pos.items() if len(v) >= MIN_VIDEOS}
    if len(means) < 3:
        return None
    worst_k = max(means, key=means.get)
    rest = [m for k, m in means.items() if k != worst_k]
    # material: clearly above the other positions AND a real share of the starting audience
    if not (means[worst_k] >= 0.08 and means[worst_k] >= 1.5 * statistics.median(rest)):
        return None
    rows = sorted(by_pos[worst_k], key=lambda x: x["loss"])
    held, lost = rows[:2], rows[-3:][::-1]
    return {"position": worst_k, "videos": len(rows), "mean_loss": round(means[worst_k], 3),
            "other_positions_median": round(statistics.median(rest), 3),
            "mean_start_s": round(statistics.mean(x["start"] for x in rows), 1),
            "mean_words_lost": round(statistics.mean(_words(x["text"]) for x in lost), 1),
            "mean_words_held": round(statistics.mean(_words(x["text"]) for x in held), 1),
            "lost": lost, "held": held}


def _ex(b: dict) -> str:
    return f'"{(b["text"] or "").strip()[:140]}" (on screen: {b.get("subject") or "?"}, {b.get("framing") or "?"})'


def directive_text(p: dict) -> str:
    k = p["position"] + 1
    held_pts = ", ".join(f"{b['loss'] * 100:.0f}" for b in p["held"])
    text = (
        f"MEASURED ON THIS CHANNEL ({p['videos']} shorts, real retention curve matched second-by-second to what was said "
        f"and shown): beat {k} (about {p['mean_start_s']}s in) loses the most viewers — {p['mean_loss'] * 100:.0f} points of "
        f"the starting audience on average, against a median of {p['other_positions_median'] * 100:.0f} for every other "
        f"beat. Lines that LOST the most viewers there: " + "; ".join(_ex(b) for b in p["lost"]) + ". "
        f"The same beat position losing the FEWEST viewers ({held_pts} points): " + "; ".join(_ex(b) for b in p["held"]) + ". "
        f"Write beat {k} so it escalates like the lower-loss examples rather than the high-loss ones"
    )
    if abs(p["mean_words_lost"] - p["mean_words_held"]) >= 3:        # only cite a trait when it actually differs
        text += f" (high-loss lines averaged {p['mean_words_lost']} words, low-loss lines {p['mean_words_held']})"
    return text + f". This is a measured pattern on this channel, not a style preference."


def learn_channel(channel_id: str, *, db_path: Optional[Path] = None, frames: bool = True) -> dict:
    """One full pass for a channel. Never raises."""
    summary = {"channel_id": channel_id, "videos": 0, "moments": 0, "directive": None}
    try:
        from hypogeum.channel_learning import get_active_directives, record_directive
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            analysis = analyse_channel(conn, channel_id, frames=frames)
            summary["videos"] = len(analysis["videos"])
            summary["moments"] = store(conn, channel_id, analysis)
            conn.commit()
        finally:
            conn.close()
        p = derive_pattern(analysis)
        if p:
            text = directive_text(p)
            active = [d for d in get_active_directives(channel_id, dimension=DIRECTIVE_DIMENSION, db_path=db_path)]
            if not active or active[0]["directive_text"] != text:
                record_directive(channel_id, DIRECTIVE_DIMENSION, "change", text, db_path=db_path,
                                 source_episode=",".join(sorted({v["episode_id"] for v in analysis["videos"].values()}))[:200])
                summary["directive"] = hashlib.sha256(text.encode()).hexdigest()[:10]
            summary["pattern_position"] = p["position"] + 1
    except Exception as exc:  # noqa: BLE001 -- learning must never break the worker
        log.error("moment_learning_failed", channel_id=channel_id, error=str(exc)[:200])
    return summary
