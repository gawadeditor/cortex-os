"""
hypogeum/revival_actions.py — turn Revival Engine findings into proposed actions, and measure what each one did.

Step 3 of the Revival Engine. The factory PROPOSES; the operator decides; nothing here edits a live post:

    propose_actions(channel_id)            -> new `proposed` rows from hypogeum.revival_engine (idempotent)
    decide(action_id, approve)             -> proposed -> approved | rejected
    mark_applied(action_id)                -> approved -> applied; captures the baseline metric and a control
    measure_due(channel_id=None)           -> applied for >= MEASURE_AFTER_DAYS -> measured, with net lift

Honest measurement: a lift is reported net of what the channel's OTHER posts did over the same window (the
control), and the result says "inconclusive" when the movement is within noise rather than claiming a win.
"""
from __future__ import annotations

import json
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="revival_actions")

MEASURE_AFTER_DAYS = 14
_NOISE_PCT = 10.0       # |net lift| below this is "inconclusive", not a win or a loss


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proposals(report: dict) -> List[dict]:
    out = []
    for g in report.get("buried_gems", []):
        out.append({"kind": "repackage_post", "platform": g["platform"], "target_ref": g["episode_id"],
                    "title": f"Give {g['episode_id']} a second chance on {g['platform']}",
                    "rationale": g["why"] + ". " + g["suggested_action"], "evidence": g})
    for c in report.get("packaging_candidates", []):
        out.append({"kind": "new_thumbnail_title", "platform": "youtube", "target_ref": c["video_id"],
                    "title": f"New thumbnail + title for {c['episode_id'] or c['video_id']}",
                    "rationale": f"shown {c['impressions']:,} times but clicked {c['ctr']:.1%} vs the channel's {c['channel_ctr']:.1%}",
                    "evidence": c})
    for s in report.get("source_contrast", []):
        out.append({"kind": "search_led_topic", "platform": "youtube", "target_ref": s["source"],
                    "title": "Make search-led topics for this channel",
                    "rationale": s["finding"], "evidence": s})
    for platform, p in report.get("platforms", {}).items():
        conv = next((l for l in p["layers"] if l["layer"] == "conversion" and l["status"] == "weak"), None)
        if conv:
            out.append({"kind": "conversion_cta", "platform": platform, "target_ref": platform,
                        "title": f"Turn {platform} reach into followers",
                        "rationale": conv["finding"] + ". Review the closing call-to-action and profile page.",
                        "evidence": conv["evidence"]})
    return out


def propose_actions(channel_id: str, *, db_path: Optional[Path] = None) -> List[dict]:
    """Insert actions the engine now recommends and that are not already open. Returns the newly created rows."""
    from hypogeum.db import get_connection, initialize_schema
    from hypogeum.revival_engine import diagnose_channel

    initialize_schema(db_path)
    report = diagnose_channel(channel_id, db_path=db_path)
    if not report.get("ok"):
        return []
    created = []
    with get_connection(db_path) as conn:
        for p in _proposals(report):
            # open = anything not rejected; a measured action may be proposed again only after new evidence, so skip it too
            if conn.execute("SELECT 1 FROM revival_actions WHERE channel_id=? AND kind=? AND target_ref=? AND platform IS ?",
                            (channel_id, p["kind"], p["target_ref"], p["platform"])).fetchone():
                continue
            aid = "rv_" + uuid.uuid4().hex[:10]
            conn.execute(
                "INSERT INTO revival_actions (action_id, channel_id, kind, platform, target_ref, title, rationale, evidence_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, channel_id, p["kind"], p["platform"], p["target_ref"], p["title"], p["rationale"],
                 json.dumps(p["evidence"], default=str), _now()))
            created.append({"action_id": aid, **{k: p[k] for k in ("kind", "platform", "target_ref", "title")}})
        conn.commit()
    return created


def list_actions(channel_id: str, *, db_path: Optional[Path] = None) -> List[dict]:
    from hypogeum.db import get_connection
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM revival_actions WHERE channel_id=? ORDER BY "
                            "CASE status WHEN 'proposed' THEN 0 WHEN 'approved' THEN 1 WHEN 'applied' THEN 2 ELSE 3 END, created_at DESC",
                            (channel_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("evidence_json", "baseline_json", "result_json"):
            d[k[:-5]] = json.loads(d[k]) if d.get(k) else None
        out.append(d)
    return out


def decide(action_id: str, approve: bool, *, db_path: Optional[Path] = None) -> bool:
    from hypogeum.db import get_connection
    with get_connection(db_path) as conn:
        cur = conn.execute("UPDATE revival_actions SET status=?, decided_at=? WHERE action_id=? AND status='proposed'",
                           ("approved" if approve else "rejected", _now(), action_id))
        conn.commit()
        return cur.rowcount == 1


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

def _metric(conn, a: dict) -> Optional[dict]:
    """{metric, value, control_value} for an action's target right now, or None if not measurable."""
    if a["kind"] == "new_thumbnail_title":
        from hypogeum.youtube_reach import channel_ctr
        d = channel_ctr(conn, a["channel_id"])
        v = next((x["ctr"] for x in (d or {"videos": []})["videos"] if x["video_id"] == a["target_ref"]), None)
        if d and v is not None:
            others = [x["ctr"] for x in d["videos"] if x["video_id"] != a["target_ref"] and x["ctr"] is not None]
            return {"metric": "ctr", "value": v, "control_value": statistics.median(others) if others else None}
    if a["kind"] == "repackage_post":
        from hypogeum.revival_engine import _load_units, _reach_of
        units = _load_units(conn, a["channel_id"]).get(a["platform"], [])
        mine = next((u for u in units if u["episode_id"] == a["target_ref"]), None)
        if mine and _reach_of(mine) is not None:
            others = [_reach_of(u) for u in units if u["episode_id"] != a["target_ref"] and _reach_of(u) is not None]
            return {"metric": "reach", "value": _reach_of(mine), "control_value": statistics.median(others) if others else None}
    return None


def mark_applied(action_id: str, *, db_path: Optional[Path] = None) -> bool:
    """The operator made the change: freeze the baseline now so the lift is measured from this moment."""
    from hypogeum.db import get_connection
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM revival_actions WHERE action_id=? AND status='approved'", (action_id,)).fetchone()
        if not row:
            return False
        base = _metric(conn, dict(row))
        conn.execute("UPDATE revival_actions SET status='applied', applied_at=?, baseline_json=? WHERE action_id=?",
                     (_now(), json.dumps(base) if base else None, action_id))
        conn.commit()
    return True


def _verdict(net: Optional[float]) -> str:
    if net is None:
        return "not measurable"
    return "inconclusive" if abs(net) < _NOISE_PCT else ("improved" if net > 0 else "got worse")


def measure_due(channel_id: Optional[str] = None, *, db_path: Optional[Path] = None, now: Optional[datetime] = None) -> int:
    """Close the loop on applied actions whose window has passed. Returns how many were measured."""
    from hypogeum.db import get_connection
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=MEASURE_AFTER_DAYS)).isoformat()
    done = 0
    with get_connection(db_path) as conn:
        q = "SELECT * FROM revival_actions WHERE status='applied' AND applied_at <= ?"
        args: list = [cutoff]
        if channel_id:
            q, args = q + " AND channel_id=?", args + [channel_id]
        for row in conn.execute(q, args).fetchall():
            a = dict(row)
            base = json.loads(a["baseline_json"]) if a["baseline_json"] else None
            cur = _metric(conn, a)
            result = {"metric": None, "verdict": "not measurable"}
            if base and cur and base.get("value"):
                lift = (cur["value"] / base["value"] - 1) * 100
                cl = None
                if base.get("control_value") and cur.get("control_value"):
                    cl = (cur["control_value"] / base["control_value"] - 1) * 100
                net = lift - (cl or 0.0)
                result = {"metric": cur["metric"], "before": base["value"], "after": cur["value"], "lift_pct": round(lift, 1),
                          "control_lift_pct": None if cl is None else round(cl, 1), "net_lift_pct": round(net, 1),
                          "verdict": _verdict(net)}
            conn.execute("UPDATE revival_actions SET status='measured', measured_at=?, result_json=? WHERE action_id=?",
                         (_now(), json.dumps(result), a["action_id"]))
            done += 1
        conn.commit()
    return done
