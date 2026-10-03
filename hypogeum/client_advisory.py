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
    """Top vs bottom by the ranking score (`_score`: engagement rate, or
    interactions within one platform — see generate_client_advisory).
    Quartiles when >= 8 items, else halves."""
    ordered = sorted(scored, key=lambda r: r["_score"], reverse=True)
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


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _span_weeks(dates: list[str]) -> float:
    ds = sorted(d for d in dates if d)
    if len(ds) < 2:
        return 1.0
    try:
        from datetime import datetime
        a = datetime.fromisoformat(ds[0].replace("Z", "+00:00"))
        b = datetime.fromisoformat(ds[-1].replace("Z", "+00:00"))
        return max((b - a).days / 7.0, 1.0)
    except Exception:  # noqa: BLE001
        return 1.0


def _phase_stats(rows: list[dict]) -> dict:
    dates = [r.get("published_at") for r in rows if r.get("published_at")]
    ers = [_num(r.get("engagement_rate")) for r in rows]
    rets = [_num(r.get("avg_view_pct")) for r in rows]
    return {
        "posts": len(rows),
        "engagement": _median(ers),
        "retention": _median(rets),
        "avg_views": (round(_mean(r.get("total_views") or 0 for r in rows)) if rows else None),
        "per_week": round(len(rows) / _span_weeks(dates), 2) if rows else None,
    }


def _before_after(client: dict, rows: list[dict]) -> Optional[dict]:
    ms = client.get("managed_since")
    if not ms:
        return {"available": False, "reason": "no managed_since date set on the client",
                "hint": "Set a start date (Accounts → Client settings → Managed since) to compare "
                        "results before and after it."}
    dated = [r for r in rows if r.get("published_at")]
    before = [r for r in dated if r["published_at"][:10] < ms[:10]]
    after = [r for r in dated if r["published_at"][:10] >= ms[:10]]
    if len(before) < 2 or len(after) < 2:
        return {"available": False,
                "reason": f"need >=2 posts each side of {ms[:10]} "
                          f"(have {len(before)} before, {len(after)} after)",
                "hint": f"Comparison against {ms[:10]} starts once there are at least 2 posts on "
                        f"each side — {len(before)} before, {len(after)} after so far."}
    b, a = _phase_stats(before), _phase_stats(after)
    deltas = {}
    for key in ("engagement", "retention", "avg_views", "per_week"):
        if b.get(key) is not None and a.get(key) is not None:
            deltas[key] = round(a[key] - b[key], 4)
    bits = []
    if "engagement" in deltas:
        bits.append(f"engagement {b['engagement']:.1f}% → {a['engagement']:.1f}%")
    if "per_week" in deltas:
        bits.append(f"posting {b['per_week']}/wk → {a['per_week']}/wk")
    if a.get("retention") is not None and b.get("retention") is not None:
        bits.append(f"retention {b['retention']*100:.0f}% → {a['retention']*100:.0f}%")
    return {"available": True, "managed_since": ms[:10],
            "before": b, "after": a, "deltas": deltas,
            "verdict": "Since you took over (" + ms[:10] + "): " + ", ".join(bits) + "."}


def generate_client_advisory(client_id: str, *,
                             db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    from hypogeum.dashboard_clients import _content_rows

    out: dict = {"subject": client_id, "ok": False, "reason": None,
                 "catalogue": {}, "summary": "", "what_works": [],
                 "recommendations": [], "best_time": [], "audience": {},
                 "monetization": None, "directives": [], "unused_raw_assets": None,
                 "before_after": None}

    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        out["reason"] = "no such client"
        return out

    rows = _content_rows(client_id, db_path)
    for r in rows:
        prof = cm.get_content_profile(r["content_id"], db_path=db_path)
        r["profile"] = (prof or {}).get("analysis") or {}
        r["hour"], r["dow"] = _hour_dow(r.get("published_at"))

    # before/after the operator took over (needs client.managed_since)
    out["before_after"] = _before_after(client, rows)

    scored = [r for r in rows if _num(r.get("engagement_rate")) is not None]
    for r in scored:
        r["engagement_rate"] = _num(r["engagement_rate"])
        r["_score"] = r["engagement_rate"]
    basis = {"metric": "engagement", "unit": "%", "platform": None}
    # Fallback (2026-10-03): when too few posts carry an engagement RATE but a
    # platform has enough posts with raw interactions (e.g. Instagram posts
    # published before a Business conversion — Meta gives no reach for them),
    # rank by interactions WITHIN that one platform. Counts are never compared
    # across platforms, and the summary names the basis.
    if len(scored) < _MIN_SCORED:
        by_plat: dict = {}
        for r in rows:
            if (r.get("interactions") or 0) > 0 and len(r.get("platforms") or []) == 1:
                by_plat.setdefault(r["platforms"][0], []).append(r)
        if by_plat:
            plat, prow = max(by_plat.items(), key=lambda kv: len(kv[1]))
            if len(prow) >= _MIN_SCORED:
                scored = prow
                for r in scored:
                    r["_score"] = float(r["interactions"])
                basis = {"metric": "interactions", "unit": "", "platform": plat}
    out["catalogue"] = {"total": len(rows), "scored": len(scored),
                        "with_metrics": sum(1 for r in rows if r.get("has_metrics")),
                        "basis": basis}

    # context blocks are always safe to attach
    _attach_context(out, client_id, db_path)

    ba = out["before_after"]
    if ba and ba.get("available"):
        out["what_works"].append({
            "axis": "before vs after you took over",
            "finding": ba["verdict"],
            "evidence": f"{ba['before']['posts']} posts before {ba['managed_since']}, "
                        f"{ba['after']['posts']} after."})
        if ba["deltas"].get("engagement", 0) < 0:
            out["recommendations"].append(
                "Engagement is down since takeover — revisit hook and thumbnail on recent posts.")

    if len(scored) < _MIN_SCORED:
        out["reason"] = f"only {len(scored)} posts have analytics — need {_MIN_SCORED} to compare"
        out["summary"] = (
            (ba["verdict"] + " ") if (ba and ba.get("available")) else ""
        ) + (
            f"{client['name']} has {len(rows)} tracked posts, "
            f"{out['catalogue']['with_metrics']} with numbers, but not yet {_MIN_SCORED} on one "
            "comparable basis. Once a few more have metrics, Cortex can tell you "
            "what the best ones have in common.")
        out["ok"] = bool(ba and ba.get("available"))
        return out

    top, bottom = _split(scored)
    out["catalogue"].update(top_n=len(top), bottom_n=len(bottom))
    top_er, bot_er = _mean(r["_score"] for r in top), _mean(r["_score"] for r in bottom)
    if basis["metric"] == "engagement":
        _vs = f"top posts average {top_er:.1f}% engagement vs {bot_er:.1f}% for the weakest"
    else:
        from hypogeum.dashboard_humanize import friendly_platform
        _vs = (f"on {friendly_platform(basis['platform'])}, top posts average "
               f"{top_er:,.0f} interactions (likes + comments + shares + saves) vs {bot_er:,.0f} "
               f"for the weakest — ranked by raw counts because these posts have no reach data")

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
        tw, bw = tcl.most_common(1)[0][0], bcl.most_common(1)[0][0]
        _cap = lambda v: "no caption" if v == "none" else f"a {v} caption"
        works.append({
            "axis": "caption length",
            "finding": f"Top posts tend to have {_cap(tw)}; weaker ones {_cap(bw)}.",
            "evidence": f"{sum(tcl.values())} vs {sum(bcl.values())} profiled captions."})
        if tw != "none":
            recs.append(f"Write {tw} captions.")

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

    # ---- unused raw material (P5) ----
    ura = out.get("unused_raw_assets")
    if ura and ura.get("count"):
        works.append({
            "axis": "raw material",
            "finding": f"{ura['count']} of {ura['total']} registered source assets "
                       "have not been used in a post yet.",
            "evidence": "client asset registry."})
        recs.append(f"Cut new posts from the {ura['count']} unused assets on file "
                    f"({', '.join(ura['labels'])}).")

    # ---- monetization gap (single closest platform) ----
    mon = out.get("monetization") or {}
    gap_line = _closest_monetization_gap(mon)
    if gap_line:
        recs.append(gap_line)

    _ba_pre = (ba["verdict"] + " ") if (ba and ba.get("available")) else ""
    if works:
        out["summary"] = _ba_pre + (
            f"{client['name']}: {_vs}. {len(works)} clear "
            f"difference{'s' if len(works) != 1 else ''} between them — see below.")
    else:
        out["summary"] = _ba_pre + (
            f"{client['name']}: {_vs}, but nothing yet separates them on a "
            "single measurable axis. More posts (and thumbnails to analyse) will sharpen this.")
        out["reason"] = ("The best and weakest posts don't yet differ clearly on timing, opening line, "
                         "image or caption length. Content analysis of each post (and more recent posts) "
                         "is what surfaces these differences.")

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
        from hypogeum.publishing.zernio_aggregate_sync import reliable_best_time_slots
        slots = reliable_best_time_slots((bt or {}).get("payload"))[:5] if bt else []
        out["best_time"] = [
            {"day": _DOW[int(s["day_of_week"])] if 0 <= int(s["day_of_week"]) < 7 else "?",
             "hour": int(s["hour"]), "engagement": round(s.get("avg_engagement") or 0, 1),
             "posts": s.get("post_count")}
            for s in slots]
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.monetization_eligibility import compute_client_monetization
        mon = compute_client_monetization(client_id, db_path=db_path)
        # Only platforms this client actually has an account on — a personal
        # brand on LinkedIn + Instagram shouldn't see YouTube/TikTok payout bars.
        from hypogeum import client_manager as _cm
        mine = {a["platform"] for a in _cm.list_managed_accounts(client_id, db_path=db_path)}
        if isinstance(mon, dict) and isinstance(mon.get("platforms"), dict):
            mon = {**mon, "platforms": {k: v for k, v in mon["platforms"].items() if k in mine}}
        out["monetization"] = mon
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.channel_learning import list_channel_changelog
        out["directives"] = list_channel_changelog(client_id, limit=15, db_path=db_path) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum import client_manager as cm
        assets = cm.list_client_assets(client_id, db_path=db_path)
        unused = [a for a in assets if not a.get("used")]
        if unused:
            out["unused_raw_assets"] = {
                "count": len(unused), "total": len(assets),
                "labels": [a["label"] for a in unused[:6]],
            }
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
