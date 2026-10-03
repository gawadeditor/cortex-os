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


# ---------------------------------------------------------------------------
# Per-platform advice (2026-10-03). The catalogue-wide comparison above needs
# content profiles before it can say anything; this reads what each platform's
# own numbers already show — cadence, dormancy, format, caption and language —
# and turns it into platform-specific actions. Every finding carries its sample
# size; an axis is only reported with >= 3 posts per side and a clear gap.
# ---------------------------------------------------------------------------

_PA_MIN_N = 3
_PA_GAP = 1.3            # one side must beat the other by 30%+
_DORMANT_DAYS = 180


def _lang_of(text: str) -> Optional[str]:
    import re
    t = (text or "").strip()
    if not t:
        return None
    return "Arabic" if re.search(r"[؀-ۿ]", t) else "English"


def _platform_score(rows: list, platform: str) -> tuple:
    """(field, label) the platform's posts are compared on."""
    if sum(1 for r in rows if r.get("engagement_rate") is not None) >= _PA_MIN_N:
        return "engagement_rate", "engagement rate"
    if platform == "linkedin":
        return "total_impressions", "impressions"
    return "interactions", "interactions"


def _compare_groups(rows: list, key, field: str, label: str, axis: str) -> Optional[dict]:
    groups: dict = {}
    for r in rows:
        g = key(r)
        v = r.get(field)
        if g is None or v is None:
            continue
        groups.setdefault(g, []).append(float(v))
    groups = {g: v for g, v in groups.items() if len(v) >= _PA_MIN_N}
    if len(groups) < 2:
        return None
    ranked = sorted(groups.items(), key=lambda kv: statistics.fmean(kv[1]), reverse=True)
    (best, bv), (worst, wv) = ranked[0], ranked[-1]
    bm, wm = statistics.fmean(bv), statistics.fmean(wv)
    if wm <= 0 or bm / wm < _PA_GAP:
        return None
    fmt = (lambda x: f"{x:.1f}%") if field == "engagement_rate" else (lambda x: f"{x:,.0f}")
    return {"axis": axis, "best": best, "worst": worst,
            "finding": f"{best} posts average {fmt(bm)} {label} vs {fmt(wm)} for {worst}.",
            "evidence": f"{len(bv)} vs {len(wv)} posts."}


def generate_platform_advice(client_id: str, *, db_path: Optional[Path] = None) -> dict:
    """{platform: {posts, last_post, posts_365d, findings: [...], actions: [...],
    audience: str|None, basis: str}} for every platform the client has an
    account on. Deterministic — no LLM."""
    from datetime import datetime, timezone
    from hypogeum import client_manager as cm
    from hypogeum.dashboard_clients import _content_rows, get_client_linkedin_snapshot

    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return {}
    strategy = (client.get("brief") or {}).get("brand_strategy") or {}
    plats = sorted({a["platform"] for a in cm.list_managed_accounts(client_id, db_path=db_path)})
    rows = _content_rows(client_id, db_path)
    now = datetime.now(timezone.utc)
    out: dict = {}
    for plat in plats:
        pr = [r for r in rows if r.get("platform") == plat]
        dates = sorted((r.get("published_at") or "")[:10] for r in pr if r.get("published_at"))
        last = dates[-1] if dates else None
        days_since = (now.date() - datetime.fromisoformat(last).date()).days if last else None
        p365 = sum(1 for d in dates if (now.date() - datetime.fromisoformat(d).date()).days <= 365)
        field, label = _platform_score(pr, plat)
        findings, actions = [], []

        # cadence / dormancy
        if days_since is not None and days_since > _DORMANT_DAYS:
            findings.append({"axis": "activity", "finding": f"No posts since {last} ({days_since // 30} months).",
                             "evidence": f"{len(pr)} posts on record."})
        target = ((strategy.get("cadence") or {}).get("posts_per_week"))
        if p365 < 26 and not (days_since is not None and days_since > _DORMANT_DAYS):
            findings.append({"axis": "cadence", "finding": f"Only {p365} post{'s' if p365 != 1 else ''} in the last 12 months.",
                             "evidence": "the algorithm rewards consistency; under 1 post / fortnight rarely builds reach."})

        # format, caption, language
        for cmp in (
            _compare_groups(pr, lambda r: (r.get("media_type") or None) and
                            {"carousel_album": "Carousel", "image": "Single-image", "video": "Video",
                             "reels": "Reels"}.get(r["media_type"], r["media_type"]),
                            field, label, "format"),
            _compare_groups(pr, lambda r: "Captioned" if (r.get("caption") or "").strip() else "No-caption",
                            field, label, "caption") if plat != "linkedin" else None,
            _compare_groups(pr, lambda r: _lang_of(r.get("caption") or r.get("title") or ""),
                            field, label, "language"),
        ):
            if cmp:
                findings.append(cmp)

        # LinkedIn: the two-post language signal is too small for the >=3 rule,
        # but reach vs engagement disagree there — report it as a labelled hint.
        if plat == "linkedin":
            langs: dict = {}
            for r in pr:
                lg = _lang_of(r.get("title") or "")
                if lg and r.get("total_impressions"):
                    langs.setdefault(lg, []).append(r)
            if "Arabic" in langs and "English" in langs:
                def _best(lst, f):
                    vals = [x.get(f) for x in lst if x.get(f) is not None]
                    return max(vals) if vals else None
                ar_i, en_i = _best(langs["Arabic"], "total_impressions"), _best(langs["English"], "total_impressions")
                ar_e, en_e = _best(langs["Arabic"], "engagement_rate"), _best(langs["English"], "engagement_rate")
                if None not in (ar_i, en_i, ar_e, en_e):
                    findings.append({"axis": "language (early signal)",
                                     "finding": f"Best Arabic post: {ar_i:,} impressions, {ar_e:.1f}% engagement. "
                                                f"Best English post: {en_i:,} impressions, {en_e:.1f}% engagement.",
                                     "best": "English" if en_e > ar_e else "Arabic",
                                     "evidence": f"{len(langs['Arabic'])} Arabic vs {len(langs['English'])} English posts — "
                                                 "too few to be sure, keep testing."})

        # actions
        role = ((strategy.get("platforms") or {}).get(plat) or {}).get("role")
        from hypogeum.dashboard_clients import calendars_of, default_month
        _cals = calendars_of(client.get("brief") or {})
        cal_posts = (_cals.get(default_month(_cals) or "") or {}).get("posts") or []
        planned = sum(1 for cp in cal_posts if plat in (cp.get("platforms") or []))
        if planned:
            per_week = max(1, round(planned / 4))
            actions.append(f"Post {per_week}× a week here, as the content calendar plans ({planned} posts this month).")
        elif target:
            actions.append(f"Post at least once a week here (plan: {target} posts/week across platforms).")
        for f in findings:
            if f["axis"] == "format" and f.get("best"):
                actions.append(f"Lean on {f['best'].lower()} posts — they outperform {f['worst'].lower()} on this account.")
            if f["axis"] == "caption" and f.get("best") == "Captioned":
                actions.append("Always write a caption — captioned posts clearly beat bare images here.")
            if f["axis"] == "language" and f.get("best"):
                actions.append(f"Write in {f['best']} — it performs better here.")
            elif f["axis"] == "language (early signal)" and f.get("best"):
                actions.append(f"Write in {f['best']} — the early signal points there; "
                               "keep watching engagement as more posts land.")
            if f["axis"] == "activity":
                actions.append("Restart with a consistent run: the first 4–6 posts reset what followers expect from you.")
        if plat == "linkedin":
            actions.append("Lead with one concrete decision or result per post (a cut, a grade, an award) — "
                           "your audience is senior media professionals.")
        if plat == "instagram":
            actions.append("Use Instagram as the visual portfolio: before/after and AI clips, carousels of stills.")

        audience = None
        if plat == "linkedin":
            snap = get_client_linkedin_snapshot(client_id, db_path=db_path)
            if snap:
                ind = (snap["audience"].get("Industry") or [{}])[0]
                sen = (snap["audience"].get("Seniority") or [{}])[0]
                audience = (f"{snap['followers_total']:,} followers; top industry {ind.get('value')} ({ind.get('pct_raw')}), "
                            f"top seniority {sen.get('value')} ({sen.get('pct_raw')}).")
        else:
            try:
                from hypogeum.audience_profile import summarize_observed_audience
                audience = summarize_observed_audience(client_id, db_path=db_path, for_display=True)
            except Exception:  # noqa: BLE001
                audience = None

        seen = set()
        actions = [a for a in actions if not (a in seen or seen.add(a))]
        out[plat] = {"posts": len(pr), "last_post": last, "posts_365d": p365, "basis": label,
                     "role": role, "findings": findings, "actions": actions, "audience": audience}
    return out
