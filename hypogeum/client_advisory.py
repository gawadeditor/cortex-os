"""
hypogeum/client_advisory.py — deterministic advisory for a Cortex Advisory
client's published catalogue. Sibling of hypogeum.comparative_insights, same
honesty discipline: only report a pattern when the top performers and the
bottom performers genuinely differ on exactly that axis, and never invent a
number.

No LLM call. Reads content_items + client-scoped platform_analytics +
content_profiles (P3) + client-scoped rollups/audience + client monetization.

Public API:
    generate_client_advisory(client_id, *, db_path=None) -> dict
      {
        subject, ok, reason,
        catalogue: {total, scored, top_n, bottom_n},
        summary: "<plain-language>",
        what_works: [{axis, finding, evidence}],
        recommendations: [str],
        best_time: [...], audience: {...}, monetization: {...},
        directives: [...],           # operator directives recorded for this client
        unused_raw_assets: None,     # filled by P5
      }
"""
from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="client_advisory")

_MIN_SCORED = 4
_HOOK_GAP = 1.5           # 1-10 scale
_THUMB_GAP = 1.5
_TAG_SHARE_GAP = 0.40    # top share minus bottom share
_TIMING_GAP_HOURS = 2.0
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hour_dow(published_at: Optional[str]):
    if not published_at:
        return None, None
    try:
        from datetime import datetime
        s = published_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.hour, dt.weekday()
    except Exception:  # noqa: BLE001
        return None, None


def _split(scored: list[dict]) -> tuple[list[dict], list[dict]]:
    """Top vs bottom by engagement_rate. Quartiles when >= 8 items, else halves."""
    ordered = sorted(scored, key=lambda r: r["engagement_rate"], reverse=True)
    n = len(ordered)
    k = max(2, n // 4) if n >= 8 else n // 2
    return ordered[:k], ordered[-k:]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def _tag_share(rows: list[dict], key: str) -> Counter:
    c: Counter = Counter()
    for r in rows:
        for t in (r.get("profile") or {}).get(key, []) or []:
            c[t] += 1
    total = sum(c.values()) or 1
    return Counter({t: v / total for t, v in c.items()})


def generate_client_advisory(client_id: str, *,
                             db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    from hypogeum.dashboard_clients import _content_rows

    out: dict = {"subject": client_id, "ok": False, "reason": None,
                 "catalogue": {}, "summary": "", "what_works": [],
                 "recommendations": [], "best_time": [], "audience": {},
                 "monetization": None, "directives": [], "unused_raw_assets": None}

    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        out["reason"] = "no such client"
        return out

    rows = _content_rows(client_id, db_path)
    for r in rows:
        prof = cm.get_content_profile(r["content_id"], db_path=db_path)
        r["profile"] = (prof or {}).get("analysis") or {}
        r["hour"], r["dow"] = _hour_dow(r.get("published_at"))

    scored = [r for r in rows if _num(r.get("engagement_rate")) is not None]
    for r in scored:
        r["engagement_rate"] = _num(r["engagement_rate"])
    out["catalogue"] = {"total": len(rows), "scored": len(scored)}

    # context blocks are always safe to attach
    _attach_context(out, client_id, db_path)

    if len(scored) < _MIN_SCORED:
        out["reason"] = f"only {len(scored)} posts have analytics — need {_MIN_SCORED} to compare"
        out["summary"] = (
            f"{client['name']} has {len(rows)} tracked posts, {len(scored)} with "
            "performance data. Once a few more have metrics, Cortex can tell you "
            "what the best ones have in common.")
        return out

    top, bottom = _split(scored)
    out["catalogue"].update(top_n=len(top), bottom_n=len(bottom))
    top_er, bot_er = _mean(r["engagement_rate"] for r in top), _mean(r["engagement_rate"] for r in bottom)

    works = out["what_works"]
    recs = out["recommendations"]

    # ---- publishing time ----
    th = [r["hour"] for r in top if r["hour"] is not None]
    bh = [r["hour"] for r in bottom if r["hour"] is not None]
    if len(th) >= 2 and len(bh) >= 2:
        tm, bm = statistics.fmean(th), statistics.fmean(bh)
        if abs(tm - bm) >= _TIMING_GAP_HOURS:
            works.append({
                "axis": "publishing time",
                "finding": f"Best posts go out around {int(round(tm)):02d}:00 UTC; "
                           f"weaker ones around {int(round(bm)):02d}:00 UTC.",
                "evidence": f"{len(th)} vs {len(bh)} posts with a timestamp."})
            recs.append(f"Schedule new posts near {int(round(tm)):02d}:00 UTC "
                        f"({client['timezone']} local equivalent).")

    # ---- hook strength ----
    thk = _mean((r["profile"].get("hook") or {}).get("strength_1_10") for r in top)
    bhk = _mean((r["profile"].get("hook") or {}).get("strength_1_10") for r in bottom)
    if thk is not None and bhk is not None and (thk - bhk) >= _HOOK_GAP:
        works.append({
            "axis": "hook",
            "finding": f"Top posts open stronger — hook score {thk:.1f} vs {bhk:.1f}.",
            "evidence": "content analysis of the caption / title opener."})
        types = Counter((r["profile"].get("hook") or {}).get("type") for r in top
                        if (r["profile"].get("hook") or {}).get("type"))
        if types:
            recs.append(f"Lead with a {types.most_common(1)[0][0].replace('_', ' ')} "
                        "opener — that is what the best posts do.")

    # ---- thumbnail ----
    tt = _mean((r["profile"].get("thumbnail") or {}).get("score_1_10") for r in top
               if (r["profile"].get("thumbnail") or {}).get("present"))
    bt = _mean((r["profile"].get("thumbnail") or {}).get("score_1_10") for r in bottom
               if (r["profile"].get("thumbnail") or {}).get("present"))
    if tt is not None and bt is not None and (tt - bt) >= _THUMB_GAP:
        works.append({
            "axis": "thumbnail",
            "finding": f"Stronger thumbnails on the top posts — {tt:.1f} vs {bt:.1f} / 10.",
            "evidence": "vision read of legibility, contrast and clutter."})
        recs.append("Give the thumbnail the same care as the best performers: "
                    "one clear subject, high contrast, legible text.")

    # ---- caption length ----
    tcl = Counter((r["profile"].get("caption") or {}).get("length") for r in top
                  if (r["profile"].get("caption") or {}).get("length"))
    bcl = Counter((r["profile"].get("caption") or {}).get("length") for r in bottom
                  if (r["profile"].get("caption") or {}).get("length"))
    if tcl and bcl and tcl.most_common(1)[0][0] != bcl.most_common(1)[0][0]:
        works.append({
            "axis": "caption length",
            "finding": f"Top posts tend to use a {tcl.most_common(1)[0][0]} caption; "
                       f"weaker ones a {bcl.most_common(1)[0][0]} one.",
            "evidence": f"{sum(tcl.values())} vs {sum(bcl.values())} profiled captions."})
        recs.append(f"Write {tcl.most_common(1)[0][0]} captions.")

    # ---- format / topic tags ----
    for key, label in (("format_tags", "format"), ("topic_tags", "topic")):
        ts, bs = _tag_share(top, key), _tag_share(bottom, key)
        for tag, share in ts.most_common(4):
            if share - bs.get(tag, 0.0) >= _TAG_SHARE_GAP:
                works.append({
                    "axis": label,
                    "finding": f"'{tag}' shows up far more in the top posts "
                               f"({share*100:.0f}% vs {bs.get(tag,0)*100:.0f}%).",
                    "evidence": "content analysis tags."})
                recs.append(f"Do more '{tag}' {label} content.")
                break

    # ---- monetization gap (single closest platform) ----
    mon = out.get("monetization") or {}
    gap_line = _closest_monetization_gap(mon)
    if gap_line:
        recs.append(gap_line)

    if works:
        out["summary"] = (
            f"{client['name']}: top posts average {top_er:.1f}% engagement vs "
            f"{bot_er:.1f}% for the weakest. {len(works)} clear "
            f"difference{'s' if len(works) != 1 else ''} between them — see below.")
    else:
        out["summary"] = (
            f"{client['name']}: top posts average {top_er:.1f}% engagement vs "
            f"{bot_er:.1f}% for the weakest, but nothing yet separates them on a "
            "single measurable axis. More posts (and thumbnails to analyse) will sharpen this.")
        out["reason"] = "no axis cleared the honesty threshold"

    out["ok"] = True
    log.info("client_advisory_generated", client_id=client_id,
             scored=len(scored), findings=len(works))
    return out


def _attach_context(out: dict, client_id: str, db_path) -> None:
    try:
        from hypogeum.publishing.zernio_native_sync import get_channel_audience
        out["audience"] = get_channel_audience(client_id, db_path=db_path) or {}
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.publishing.zernio_aggregate_sync import get_rollup
        bt = get_rollup("best_time", scope=f"client:{client_id}", db_path=db_path)
        slots = sorted([s for s in (bt or {}).get("payload", [])
                        if (s.get("post_count") or 0) >= 2],
                       key=lambda s: -(s.get("avg_engagement") or 0))[:5] if bt else []
        out["best_time"] = [
            {"day": _DOW[int(s["day_of_week"])] if 0 <= int(s["day_of_week"]) < 7 else "?",
             "hour": int(s["hour"]), "engagement": round(s.get("avg_engagement") or 0, 1),
             "posts": s.get("post_count")}
            for s in slots]
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.monetization_eligibility import compute_client_monetization
        out["monetization"] = compute_client_monetization(client_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.channel_learning import list_channel_changelog
        out["directives"] = list_channel_changelog(client_id, limit=15, db_path=db_path) or []
    except Exception:  # noqa: BLE001
        pass


def _closest_monetization_gap(mon: dict) -> Optional[str]:
    best = None
    for platform, p in (mon.get("platforms") or {}).items():
        if p.get("eligible"):
            continue
        for r in p.get("requirements", []):
            if r.get("met") or not r.get("tracked") or r.get("pct") is None:
                continue
            if best is None or r["pct"] > best[1]:
                best = (f"{platform}: {r['label']} — {r['pct']:.0f}% of the way "
                        f"({r['remaining']:,.0f} {r['unit']} to payout eligibility).",
                        r["pct"])
    return best[0] if best else None
