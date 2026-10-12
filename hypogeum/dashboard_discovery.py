"""
hypogeum/dashboard_discovery.py — what the factory has LEARNED about how viewers find and leave a channel, for the
Analytics page: where YouTube views come from, what viewers searched, the moments viewers left (with the actual frame),
and the guidance the factory wrote from it. Read-only over youtube_traffic_insights / retention_moments /
channel_learning_directives. Never raises: a missing table just means an empty panel.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hypogeum.youtube_traffic import _PRETTY, derive_findings

FRAME_DIR_NAME = "retention_frames"


def _device_label(key: str) -> str:
    return {"TV": "TV", "GAME_CONSOLE": "Game console"}.get(key, key.replace("_", " ").title())


def _frame_name(path: Optional[str]) -> Optional[str]:
    return Path(path).name if path else None


def get_discovery_panel(channel_id: str, db_path=None) -> dict:
    out = {"channel_id": channel_id, "snapshot_date": None, "mix": [], "search_terms": [], "brand_terms": [],
           "subscribed": [], "devices": [], "exits": [], "directives": [], "has_data": False}
    try:
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            snap = conn.execute("SELECT MAX(snapshot_date) FROM youtube_traffic_insights WHERE channel_id = ?", (channel_id,)).fetchone()[0]
            if snap:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM youtube_traffic_insights WHERE channel_id = ? AND snapshot_date = ?", (channel_id, snap))]
                f = derive_findings(rows, channel_id)
                out["snapshot_date"] = snap
                out["mix"] = [{**m, "label": _PRETTY.get(m["source"], m["source"].replace("_", " ").title())} for m in f["mix"][:6]]
                out["search_terms"] = sorted(f["search_terms"], key=lambda r: (-r["views"], -r["avg_view_pct"]))[:12]
                out["brand_terms"] = f["brand_terms"][:4]
                out["search_share"], out["retention_gap"] = f["search_share"], f["retention_gap"]
                tot = sum(r["views"] for r in rows if r["dimension"] == "subscribed") or 0
                out["subscribed"] = [{"label": r["key"].title(), "share": (r["views"] / tot if tot else 0), "views": r["views"],
                                      "avg_view_pct": r["avg_view_pct"]} for r in rows if r["dimension"] == "subscribed"]
                dt = sum(r["views"] for r in rows if r["dimension"] == "device") or 0
                out["devices"] = sorted(({"label": _device_label(r["key"]), "share": (r["views"] / dt if dt else 0), "views": r["views"],
                                          "avg_view_pct": r["avg_view_pct"]} for r in rows if r["dimension"] == "device"),
                                        key=lambda d: -d["share"])
            ex = [dict(r) for r in conn.execute(
                "SELECT * FROM retention_moments WHERE channel_id = ? AND kind = 'exit' ORDER BY loss DESC LIMIT 9", (channel_id,))]
            seen_video = set()
            for r in ex:                                           # one card per video, biggest exits first
                if r["video_id"] in seen_video:
                    continue
                seen_video.add(r["video_id"])
                vis = {}
                try:
                    vis = json.loads(r.get("visual_json") or "{}")
                except ValueError:
                    pass
                out["exits"].append({"video_id": r["video_id"], "episode_id": r["episode_id"], "t": round((r["t0"] + r["t1"]) / 2, 1),
                                     "loss_pts": round(r["loss"] * 100), "views": r["video_views"], "spoken": r["spoken_text"],
                                     "beat": (r["beat_index"] + 1) if r["beat_index"] is not None else None,
                                     "subject": vis.get("subject"), "framing": vis.get("framing"),
                                     "frame": _frame_name(r.get("frame_path"))})
                if len(out["exits"]) >= 6:
                    break
            for dim, title in (("retention_moments", "Where viewers leave"), ("search_demand", "What viewers search for")):
                for d in conn.execute("SELECT directive_text, created_at FROM channel_learning_directives WHERE channel_id = ? "
                                      "AND dimension = ? AND active = 1 ORDER BY created_at DESC LIMIT 1", (channel_id, dim)):
                    out["directives"].append({"title": title, "text": d["directive_text"], "created_at": d["created_at"]})
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- the page must render even if a table is missing
        pass
    out["has_data"] = bool(out["mix"] or out["exits"] or out["directives"])
    return out


def get_episode_discovery(episode_id: str, db_path=None) -> dict:
    """Per-episode view for /analytics/episode/<id>: for each of the episode's YouTube videos — the retention curve, the
    moments viewers left (with the frame) and how that video was found (traffic types + search terms). Never raises."""
    out = {"videos": []}
    try:
        from hypogeum.db import get_connection, initialize_schema
        from hypogeum.retention_moments import _points
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            ch = conn.execute("SELECT channel_id FROM episode_topics WHERE episode_id = ? LIMIT 1", (episode_id,)).fetchone()
            channel_id = ch[0] if ch else None
            vids = conn.execute(
                "SELECT platform_video_id v, MAX(views) views, MAX(avg_view_pct) vp, MAX(avg_watch_time_s) aw, "
                "(SELECT q.retention_curve_json FROM platform_analytics q WHERE q.platform_video_id = pa.platform_video_id "
                " AND length(q.retention_curve_json) > 5 LIMIT 1) cur FROM platform_analytics pa WHERE episode_id = ? AND platform = 'youtube' "
                "AND source = 'api' AND platform_video_id IS NOT NULL GROUP BY platform_video_id", (episode_id,)).fetchall()
            snap = conn.execute("SELECT MAX(snapshot_date) FROM youtube_traffic_insights WHERE channel_id = ?", (channel_id,)).fetchone()[0] if channel_id else None
            for v in vids:
                item = {"video_id": v["v"], "views": v["views"], "avg_view_pct": v["vp"], "avg_watch_s": v["aw"],
                        "curve": [], "exits": [], "found_by": [], "searches": []}
                if v["cur"]:
                    try:
                        pts = _points(json.loads(v["cur"]))
                        step = max(1, len(pts) // 50)
                        item["curve"] = [round(min(y, 1.5), 3) for _, y in pts[::step]]
                    except ValueError:
                        pass
                for r in conn.execute("SELECT * FROM retention_moments WHERE video_id = ? AND kind = 'exit' ORDER BY loss DESC LIMIT 3", (v["v"],)):
                    r = dict(r)
                    try:
                        vis = json.loads(r.get("visual_json") or "{}")
                    except ValueError:
                        vis = {}
                    item["exits"].append({"t": round((r["t0"] + r["t1"]) / 2, 1), "loss_pts": round(r["loss"] * 100),
                                          "beat": (r["beat_index"] + 1) if r["beat_index"] is not None else None,
                                          "spoken": r["spoken_text"], "subject": vis.get("subject"), "framing": vis.get("framing"),
                                          "frame": _frame_name(r.get("frame_path"))})
                if snap:
                    tot = 0
                    rows = [dict(r) for r in conn.execute(
                        "SELECT subkey, views, avg_view_pct FROM youtube_traffic_insights WHERE channel_id = ? AND snapshot_date = ? "
                        "AND dimension = 'video_traffic' AND key = ? ORDER BY views DESC", (channel_id, snap, v["v"]))]
                    tot = sum(r["views"] for r in rows) or 0
                    item["found_by"] = [{"label": _PRETTY.get(r["subkey"], r["subkey"].replace("_", " ").title()),
                                         "share": (r["views"] / tot if tot else 0), "views": r["views"], "avg_view_pct": r["avg_view_pct"]}
                                        for r in rows if r["views"]][:5]
                    item["searches"] = [dict(r) for r in conn.execute(
                        "SELECT subkey AS term, views, avg_view_pct FROM youtube_traffic_insights WHERE channel_id = ? AND snapshot_date = ? "
                        "AND dimension = 'video_search' AND key = ? AND views > 0 ORDER BY views DESC LIMIT 6", (channel_id, snap, v["v"]))]
                out["videos"].append(item)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        out["videos"] = []
    return out
