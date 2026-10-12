"""
hypogeum/market_intel.py — what is working RIGHT NOW on YouTube for channels that are brand new.

The factory scans YouTube itself (YouTube Data API v3, with a channel's own OAuth token) for videos that gained the most
views recently, keeps the channels behind them that were CREATED recently (default: within a year) and already hold
millions of views, reads their recent uploads, and derives — deterministically, no LLM, no spend — what they have in
common: the niche, the title formulas, how the description is written, the hashtags and tags, Shorts vs long, how often
they upload, how many views a video gets per subscriber. The output is a report the dashboard shows and the New Channel
flow uses to suggest what to build.

    run_scan(db_path=None, *, token=None, max_units=2500) -> dict     fetch -> analyse -> store (never raises)
    analyse(channels, videos) -> dict                                  pure: the report
    latest_report(db_path=None) -> dict | None
    maybe_run_weekly(db_path=None) -> bool                             starts a background scan when one is due

Quota: search.list costs 100 units, everything else 1. The Google project's daily quota (10,000) is shared with uploads
(Postiz uploads cost 1,600 each), so a scan is capped at `max_units` (default 2,500) and scheduled right after the
quota reset (07:00 UTC). A quota error stops the scan cleanly and keeps whatever was already collected.

Lock rule: all HTTP with NO database connection open; one short write transaction at the end.
"""
from __future__ import annotations

import json
import re
import statistics
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from hypogeum.logger import get_logger

log = get_logger(agent="market_intel")

_API = "https://www.googleapis.com/youtube/v3"
_TIMEOUT = 30
MAX_CHANNEL_AGE_DAYS = 365          # "created recently"
MIN_CHANNEL_VIEWS = 1_000_000       # "already pulled millions"
SEARCH_WINDOW_DAYS = 180            # videos published within this window are searched by views
SHORT_MAX_S = 180
REPORT_EVERY_DAYS = 7
MIN_CHANNELS_PER_NICHE = 2
READ_RESERVE = 250                  # units kept back for reading channels and uploads after the searches

# Niches a faceless, AI-assisted channel can realistically build; each is a search seed. Rotated so a weekly scan covers all.
SEEDS = [
    "ancient mysteries", "history facts", "unsolved mysteries", "mythology stories", "true crime stories", "bedtime story for kids",
    "kids cartoon story", "relaxing sleep story", "scary horror stories", "space facts", "science explained", "animal facts",
    "stoicism motivation", "personal finance tips", "luxury lifestyle", "AI generated story", "what if scenarios", "geography facts",
    "trivia quiz", "life hacks", "psychology facts", "dark history", "ocean deep sea mysteries", "dinosaur facts", "inspiring true stories",
    "satisfying relaxing", "cooking shorts", "fitness tips", "tech news explained", "movie recap",
]
_HASHTAG = re.compile(r"#[\wÀ-￿]+")
_STOP = set("the a an of to in on and or for with from by at is are was were be been this that it its you your we our their how why what when who "
            "which into about than then them they he she his her not no yes can will would could should do does did has have had more most "
            "very just all any one two three new best top real full part video shorts short".split())
_EN_WORDS = set("the a an of to in on and or for with from by at is are was were be this that it you your we how why what when who which "
                "into about not can will would could do does did has have had more most just all one new".split())
_POWER = ("secret", "mystery", "shocking", "never", "truth", "hidden", "unbelievable", "forgotten", "impossible", "banned", "lost", "ancient",
          "ultimate", "insane", "terrifying", "dark", "real", "exposed", "revealed", "unsolved", "mind", "blowing")


def _now() -> datetime:
    return datetime.now(timezone.utc)


_LANG_STOP = {
    "es": set("el la los las de del y en un una que por con para es se no su al lo como más pero sus le ya o este sí porque esta".split()),
    "fr": set("le la les de des du et en un une que pour avec est pas qui dans sur au ce se ne plus par mais son sa ses il elle".split()),
    "pt": set("o a os as de do da dos das e em um uma que para com é não se por mais mas como foi ao ele ela seu sua".split()),
}
_REGION = {"en": "US", "ar": "SA", "es": "MX", "fr": "FR", "pt": "BR", "hi": "IN"}


def _script_ratio(title: str, lo: int, hi: int) -> float:
    letters = [ch for ch in (title or "") if ch.isalpha()]
    return (sum(lo <= ord(ch) <= hi for ch in letters) / len(letters)) if letters else 0.0


def _lang_ok(title: str, lang: str = "en", declared: Optional[str] = None) -> bool:
    """Does this video belong to the requested language? Declared audio/default language first, else script/stop-word heuristics."""
    if declared:
        return declared.lower().startswith(lang)
    if lang == "ar":
        return _script_ratio(title, 0x0600, 0x06FF) >= 0.5
    if lang == "hi":
        return _script_ratio(title, 0x0900, 0x097F) >= 0.5
    if lang in _LANG_STOP:
        toks = re.findall(r"[a-zà-ÿ']+", (title or "").lower())
        return len(toks) >= 2 and sum(t in _LANG_STOP[lang] for t in toks) / len(toks) >= 0.12
    return _is_english(title, None)


def _is_english(title: str, lang: Optional[str] = None) -> bool:
    """Language filter for English-language niches: the declared audio language when present, else common-word share."""
    if lang:
        return lang.lower().startswith("en")
    toks = re.findall(r"[A-Za-z']+", (title or "").lower())
    if not toks or len(toks) < 2:
        return False
    return sum(t in _EN_WORDS for t in toks) / len(toks) >= 0.12 and sum(1 for ch in (title or "") if ord(ch) > 0x24F and ch.isalpha()) == 0


def _iso_duration_s(d: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    dd, h, mi, s = (int(x or 0) for x in m.groups())
    return dd * 86400 + h * 3600 + mi * 60 + s


class _Quota(Exception):
    pass


class _Api:
    def __init__(self, token: str, max_units: int):
        self.h = {"Authorization": f"Bearer {token}"}
        self.units = 0
        self.max_units = max_units

    def get(self, path: str, params: dict, cost: int = 1) -> Optional[dict]:
        if self.units + cost > self.max_units:
            raise _Quota("scan budget reached")
        try:
            r = requests.get(f"{_API}/{path}", headers=self.h, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            log.warn("market_api_network_error", path=path, error=str(exc)[:120])
            return None
        self.units += cost
        if r.status_code == 403 and "quota" in r.text.lower():
            raise _Quota("YouTube daily quota exhausted")
        if r.status_code != 200:
            log.warn("market_api_error", path=path, status=r.status_code, body=r.text[:140])
            return None
        return r.json()


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def _video_row(it: dict) -> dict:
    sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
    text = f"{sn.get('title', '')}\n{sn.get('description', '')}"
    tags = sn.get("tags") or []
    dur = _iso_duration_s(cd.get("duration", ""))
    return {"video_ref": it["id"], "channel_ref": sn.get("channelId"), "title": sn.get("title", ""), "published_at": sn.get("publishedAt"),
            "views": int(st.get("viewCount", 0) or 0), "likes": int(st.get("likeCount", 0) or 0), "comments": int(st.get("commentCount", 0) or 0),
            "duration_s": dur, "is_short": 1 if 0 < dur <= SHORT_MAX_S else 0, "tags": tags,
            "hashtags": sorted({h.lower() for h in _HASHTAG.findall(text)}), "description_head": (sn.get("description") or "")[:600],
            "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage")}


def fetch(api: _Api, seeds: List[str], *, searches_per_seed: int = 1, max_age_days: int = MAX_CHANNEL_AGE_DAYS,
          min_views: int = MIN_CHANNEL_VIEWS, language: str = "en", progress=None) -> dict:
    """Search by recent view count for each seed, keep new channels with millions of views, read their recent uploads."""
    after = (_now() - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seed_of_video: Dict[str, str] = {}
    stopped = None
    try:
        for si, seed in enumerate(seeds):
            if api.units + 100 > api.max_units - READ_RESERVE:
                stopped = "search budget reached (kept what was found)"       # leave units to read the channels found so far
                break
            if progress:
                progress("searching", si, len(seeds), seed)
            for dur in (["short", "medium"] if searches_per_seed > 1 else ["any"]):
                params = {"part": "snippet", "type": "video", "q": seed, "publishedAfter": after, "order": "viewCount", "maxResults": 50,
                          "relevanceLanguage": language, "regionCode": _REGION.get(language, "US")}
                if dur != "any":
                    params["videoDuration"] = dur
                res = api.get("search", params, cost=100)
                for it in (res or {}).get("items", []):
                    seed_of_video.setdefault(it["id"]["videoId"], seed)
        if progress:
            progress("reading channels", len(seeds), len(seeds), "")
        # channels behind the winning videos
        vids: Dict[str, dict] = {}
        for ids in _chunks(list(seed_of_video), 50):
            for it in (api.get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids)}) or {}).get("items", []):
                vids[it["id"]] = it
        ch_ids = sorted({v["snippet"]["channelId"] for v in vids.values()})
        channels: Dict[str, dict] = {}
        for ids in _chunks(ch_ids, 50):
            for it in (api.get("channels", {"part": "snippet,statistics,topicDetails,brandingSettings", "id": ",".join(ids)}) or {}).get("items", []):
                channels[it["id"]] = it
        cutoff = _now() - timedelta(days=max_age_days)
        keep = {}
        for cid, it in channels.items():
            try:
                created = datetime.fromisoformat(it["snippet"]["publishedAt"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            views = int(it.get("statistics", {}).get("viewCount", 0) or 0)
            if created >= cutoff and views >= min_views:
                keep[cid] = it
        seed_by_channel = {}
        for vid, it in vids.items():
            seed_by_channel.setdefault(it["snippet"]["channelId"], seed_of_video[vid])
        # their recent uploads (1 unit for the playlist page + 1 per 50 videos)
        uploads: Dict[str, dict] = {}
        for ci, cid in enumerate(keep):
            if progress:
                progress("reading competitors", ci, len(keep), (keep[cid].get("snippet", {}) or {}).get("title", ""))
            pl = api.get("playlistItems", {"part": "contentDetails", "playlistId": "UU" + cid[2:], "maxResults": 50}) or {}
            vid_ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
            for it in (api.get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(vid_ids)}) or {}).get("items", []) if vid_ids else []:
                uploads[it["id"]] = it
    except _Quota as exc:
        stopped = str(exc)
        keep = locals().get("keep", {})
        uploads = locals().get("uploads", {})
        seed_by_channel = locals().get("seed_by_channel", {})
    return {"channels": keep, "videos": uploads, "seed_by_channel": seed_by_channel, "stopped": stopped, "units": api.units}


# ---------------------------------------------------------------------------
# analysis (pure)
# ---------------------------------------------------------------------------

def _title_features(title: str) -> dict:
    t = title or ""
    words = re.findall(r"[A-Za-z0-9'’]+", t)
    lower = t.lower()
    return {"len": len(t), "words": len(words), "has_number": bool(re.search(r"\d", t)), "question": "?" in t or lower.startswith(("why ", "how ", "what ", "who ", "is ", "can ")),
            "emoji": bool(re.search(r"[\U0001F300-\U0001FAFF☀-➿]", t)), "caps_word": any(w.isupper() and len(w) > 2 for w in words),
            "power": any(p in lower for p in _POWER), "hashtag_in_title": "#" in t,
            "first_word": (words[0].lower() if words else "")}


def _ngrams(titles: List[str]) -> Counter:
    c: Counter = Counter()
    for t in titles:
        ws = [w for w in re.findall(r"[a-z0-9']+", t.lower()) if w not in _STOP and len(w) > 2]
        c.update(ws)
        c.update(" ".join(p) for p in zip(ws, ws[1:]))
    return c


def _channel_stats(ch: dict, vids: List[dict]) -> dict:
    views = [v["views"] for v in vids]
    dates = sorted(datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) for v in vids if v.get("published_at"))
    span_days = max((dates[-1] - dates[0]).days, 1) if len(dates) > 1 else 0
    return {"median_views": int(statistics.median(views)) if views else 0, "top_views": max(views) if views else 0,
            "shorts_share": round(sum(v["is_short"] for v in vids) / len(vids), 2) if vids else 0.0,
            "uploads_per_week": round(len(dates) / span_days * 7, 1) if span_days else 0.0, "videos_read": len(vids)}


def _template_of(title: str) -> str:
    """A title's skeleton: numbers -> N, the first three words kept, the rest elided ('Why N Facts ...')."""
    t = re.sub(r"\d+", "N", title or "")
    t = re.sub(r"#\w+", "", t)
    ws = re.findall(r"[A-Za-z'N]+", t)
    head = " ".join(ws[:3])
    return (head + " …") if len(ws) > 3 else head


def _how_they_work(chs: List[dict], vs: List[dict]) -> dict:
    """The way the proven channels operate: when they publish, how long videos are, the title skeletons they reuse, how
    the description closes, and how fast they got to millions."""
    hours, days = Counter(), Counter()
    for v in vs:
        try:
            d = datetime.fromisoformat((v.get("published_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        hours[d.hour] += 1
        days[d.weekday()] += 1
    durs = [v["duration_s"] for v in vs if v.get("duration_s")]
    templ = Counter(_template_of(v["title"]) for v in vs if v["title"])
    ctas = sum(1 for v in vs if re.search(r"subscribe|follow|like and|comment|link in|check out|watch more", v["description_head"], re.I))
    ages = [(_now() - datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).days for c in chs if c.get("created_at")]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    ex_by_t: Dict[str, str] = {}
    for v in sorted(vs, key=lambda v: -v["views"]):
        ex_by_t.setdefault(_template_of(v["title"]), v["title"])
    return {"best_hours_utc": [h for h, _ in hours.most_common(3)], "best_days": [names[d] for d, _ in days.most_common(3)],
            "median_length_s": int(statistics.median(durs)) if durs else None,
            "under_60s_pct": round(100 * sum(1 for d in durs if d <= 60) / len(durs)) if durs else None,
            "title_templates": [{"pattern": t, "count": c, "example": ex_by_t.get(t, "")} for t, c in templ.most_common(12) if c >= 2 and len(t) >= 6][:5],
            "cta_pct": round(100 * ctas / len(vs)) if vs else 0,
            "median_days_old": int(statistics.median(ages)) if ages else None,
            "views_per_video_top25_pct": int(statistics.quantiles([v["views"] for v in vs], n=4)[2]) if len(vs) >= 8 else None}


def analyse(channels: List[dict], videos: List[dict]) -> dict:
    """channels: [{channel_ref, title, created_at, subscribers, total_views, video_count, niche_seed, keywords, country}], videos: rows with tags/hashtags lists."""
    by_ch: Dict[str, List[dict]] = {}
    for v in videos:
        by_ch.setdefault(v["channel_ref"], []).append(v)
    niches: Dict[str, List[dict]] = {}
    for ch in channels:
        vs_c = by_ch.get(ch["channel_ref"], [])
        toks = [w[:5] for w in re.findall(r"[a-z]+", (ch.get("niche_seed") or "").lower()) if w not in _STOP and len(w) > 3]
        if toks and vs_c:
            hit = sum(1 for v in vs_c if any(t in f"{v['title']} {' '.join(v.get('tags', []))} {' '.join(v['hashtags'])}".lower() for t in toks))
            if hit / len(vs_c) < 0.35:
                continue                      # surfaced by a seed it does not really belong to (e.g. anime under 'unsolved mysteries')
        ch = {**ch, **_channel_stats(ch, vs_c)}
        ch["views_per_sub"] = round(ch["total_views"] / ch["subscribers"], 1) if ch.get("subscribers") else None
        niches.setdefault(ch.get("niche_seed") or "other", []).append(ch)
    report_niches = []
    for seed, chs in niches.items():
        vs = [v for c in chs for v in by_ch.get(c["channel_ref"], [])]
        if not vs:
            continue
        feats = [_title_features(v["title"]) for v in vs]
        n = len(feats)
        # hashtags that several DIFFERENT channels use (one channel spamming a tag is not a convention)
        ch_of_tag: Dict[str, set] = {}
        for v in vs:
            for h in v["hashtags"]:
                ch_of_tag.setdefault(h, set()).add(v["channel_ref"])
        multi = {h for h, cs in ch_of_tag.items() if len(cs) >= 2} if len(chs) >= 3 else set(ch_of_tag)
        hashtags = Counter(h for v in vs for h in v["hashtags"] if h in multi)
        tags = Counter(t.lower() for v in vs for t in v.get("tags", []))
        first = Counter(f["first_word"] for f in feats if f["first_word"])
        top_videos = sorted(vs, key=lambda v: -v["views"])[:5]
        ch_titles = {c["channel_ref"]: c["title"] for c in chs}
        report_niches.append({
            "niche": seed, "channels": len(chs), "videos_read": n,
            "median_channel_views": int(statistics.median(c["total_views"] for c in chs)),
            "median_subscribers": int(statistics.median(c["subscribers"] for c in chs)) if any(c.get("subscribers") for c in chs) else None,
            "median_views_per_video": int(statistics.median(v["views"] for v in vs)),
            "median_age_days": int(statistics.median(
                (_now() - datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).days for c in chs if c.get("created_at"))) if chs else None,
            "shorts_share": round(statistics.mean(c["shorts_share"] for c in chs), 2),
            "uploads_per_week": round(statistics.median(c["uploads_per_week"] for c in chs), 1),
            "title": {"avg_len": round(statistics.mean(f["len"] for f in feats)), "avg_words": round(statistics.mean(f["words"] for f in feats), 1),
                      "number_pct": round(100 * sum(f["has_number"] for f in feats) / n), "question_pct": round(100 * sum(f["question"] for f in feats) / n),
                      "emoji_pct": round(100 * sum(f["emoji"] for f in feats) / n), "caps_word_pct": round(100 * sum(f["caps_word"] for f in feats) / n),
                      "power_word_pct": round(100 * sum(f["power"] for f in feats) / n), "hashtag_in_title_pct": round(100 * sum(f["hashtag_in_title"] for f in feats) / n),
                      "top_openers": first.most_common(5), "top_terms": [k for k, _ in _ngrams([v["title"] for v in vs]).most_common(12)]},
            "description": {"avg_len": round(statistics.mean(len(v["description_head"]) for v in vs)),
                            "with_hashtags_pct": round(100 * sum(1 for v in vs if v["hashtags"]) / n),
                            "avg_hashtags": round(statistics.mean(len(v["hashtags"]) for v in vs), 1),
                            "with_link_pct": round(100 * sum(1 for v in vs if "http" in v["description_head"]) / n)},
            "how": _how_they_work(chs, vs),
            "hashtags": hashtags.most_common(12), "tags": tags.most_common(12), "tags_per_video": round(statistics.mean(len(v.get("tags", [])) for v in vs), 1),
            "examples": [{"title": v["title"], "views": v["views"], "channel": ch_titles.get(v["channel_ref"], ""), "video": v["video_ref"]} for v in top_videos],
            "channel_examples": [{"title": c["title"], "ref": c["channel_ref"], "created_at": (c.get("created_at") or "")[:10], "subscribers": c.get("subscribers"),
                                  "total_views": c["total_views"], "views_per_sub": c["views_per_sub"], "shorts_share": c["shorts_share"]}
                                 for c in sorted(chs, key=lambda c: -c["total_views"])[:6]],
        })
    report_niches.sort(key=lambda n: (-n["channels"], -n["median_views_per_video"]))
    return {"niches": report_niches, "channels_total": len(channels), "videos_total": len(videos),
            "suggestions": _suggestions(report_niches)}


_POOR_FIT = ("gaming", "reaction", "news", "vlog", "unboxing")


def _suggestions(niches: List[dict]) -> List[dict]:
    """Deterministic: rank niches that several NEW channels have already proven, and say how to build one."""
    out = []
    for n in niches:
        if n["channels"] < MIN_CHANNELS_PER_NICHE or any(w in n["niche"] for w in _POOR_FIT):
            continue
        t, d = n["title"], n["description"]
        fmt = "Shorts" if n["shorts_share"] >= 0.6 else ("long-form" if n["shorts_share"] <= 0.2 else "a Shorts + long-form mix")
        how = [f"Format: mostly {fmt}; {n['uploads_per_week']} uploads a week is what the proven channels do.",
               f"Titles: ~{t['avg_len']} characters ({t['avg_words']} words); {t['number_pct']}% contain a number, {t['question_pct']}% are questions, "
               f"{t['power_word_pct']}% use a power word; most common openers: {', '.join(w for w, _ in t['top_openers'][:4])}.",
               f"Description: ~{d['avg_len']} characters, {d['with_hashtags_pct']}% carry hashtags (avg {d['avg_hashtags']}).",
               "Hashtags: " + (", ".join(h for h, _ in n["hashtags"][:8]) or "none worth copying") + ".",
               "Recurring title terms: " + ", ".join(n["title"]["top_terms"][:8]) + "."]
        out.append({"niche": n["niche"], "why": f"{n['channels']} channels created in the last year already hold a median {n['median_channel_views']:,} views; "
                                                f"a video gets a median {n['median_views_per_video']:,} views.", "how": how,
                    "score": n["channels"] * 10 + min(n["median_views_per_video"] / 50_000, 10)})
    return sorted(out, key=lambda s: -s["score"])[:8]


# ---------------------------------------------------------------------------
# store / orchestrate
# ---------------------------------------------------------------------------

def _rows_from_fetch(fx: dict, language: str = "en") -> tuple:
    channels, videos = [], []
    for cid, it in fx["channels"].items():
        sn, st = it.get("snippet", {}), it.get("statistics", {})
        channels.append({"channel_ref": cid, "title": sn.get("title", ""), "created_at": sn.get("publishedAt"), "country": sn.get("country"),
                         "subscribers": int(st["subscriberCount"]) if st.get("subscriberCount") else None, "total_views": int(st.get("viewCount", 0) or 0),
                         "video_count": int(st.get("videoCount", 0) or 0), "keywords": it.get("brandingSettings", {}).get("channel", {}).get("keywords", ""),
                         "topic_categories": it.get("topicDetails", {}).get("topicCategories", []), "niche_seed": fx["seed_by_channel"].get(cid)})
    for it in fx["videos"].values():
        videos.append(_video_row(it))
    # English-language niches only: drop channels whose recent uploads are mostly not English
    en_share: Dict[str, List[bool]] = {}
    for v in videos:
        en_share.setdefault(v["channel_ref"], []).append(_lang_ok(v["title"], language, v["language"]))
    keep_refs = {c for c, xs in en_share.items() if xs and sum(xs) / len(xs) >= 0.5}
    channels = [c for c in channels if c["channel_ref"] in keep_refs]
    videos = [v for v in videos if v["channel_ref"] in keep_refs]
    return channels, videos


def _store(conn, channels: List[dict], videos: List[dict], report: dict, units: int) -> str:
    now = _now().isoformat()
    by_ch: Dict[str, List[dict]] = {}
    for v in videos:
        by_ch.setdefault(v["channel_ref"], []).append(v)
    for c in channels:
        st = _channel_stats(c, by_ch.get(c["channel_ref"], []))
        old = conn.execute("SELECT first_seen FROM market_channels WHERE channel_ref = ?", (c["channel_ref"],)).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO market_channels (channel_ref, title, created_at, country, subscribers, total_views, video_count, keywords, "
            "topic_categories_json, niche_seed, median_views, shorts_share, uploads_per_week, first_seen, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["channel_ref"], c["title"], c["created_at"], c["country"], c["subscribers"], c["total_views"], c["video_count"], c["keywords"],
             json.dumps(c["topic_categories"]), c["niche_seed"], st["median_views"], st["shorts_share"], st["uploads_per_week"], old[0] if old else now, now))
    for v in videos:
        conn.execute(
            "INSERT OR REPLACE INTO market_videos (video_ref, channel_ref, title, published_at, views, likes, comments, duration_s, is_short, tags_json, "
            "hashtags_json, description_head, language, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v["video_ref"], v["channel_ref"], v["title"], v["published_at"], v["views"], v["likes"], v["comments"], v["duration_s"], v["is_short"],
             json.dumps(v["tags"]), json.dumps(v["hashtags"]), v["description_head"], v["language"], now))
    rid = "mr_" + uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO market_reports (report_id, created_at, units_used, channels_found, report_json) VALUES (?,?,?,?,?)",
                 (rid, now, units, len(channels), json.dumps(report, ensure_ascii=False)))
    return rid


def _channel_seeds(db_path) -> List[str]:
    """Seeds from the factory's own channel briefs (their content pillars) so the scan always covers our own niches."""
    out: List[str] = []
    try:
        from hypogeum.channel_creative_brief import channel_brief_for
        from hypogeum.channel_config import list_channel_profiles
        for cp in list_channel_profiles(db_path=db_path):
            b = channel_brief_for(getattr(cp, "channel_id", ""))
            out += [str(p) for p in (b.get("content_pillars") or [])[:2]]
            if b.get("genre"):
                out.append(str(b["genre"]))
    except Exception:  # noqa: BLE001
        pass
    return [s for s in dict.fromkeys(x.strip().lower() for x in out) if 2 < len(s) < 60]


def pick_seeds(db_path=None, n: int = 20) -> List[str]:
    """Own-niche seeds first, then a weekly rotation through SEEDS so successive scans cover more of the market."""
    week = int(_now().strftime("%V"))
    k = week % len(SEEDS)
    rotated = SEEDS[k:] + SEEDS[:k]
    return list(dict.fromkeys(_channel_seeds(db_path) + rotated))[:n]


def get_token(db_path=None) -> tuple:
    """(access_token, error) from the first factory channel with a working YouTube authorization."""
    from hypogeum.channel_config import list_channel_profiles
    from hypogeum.publishing.youtube_privacy import _access_token
    last = None
    for cp in list_channel_profiles(db_path=db_path):
        try:
            return _access_token(getattr(cp, "channel_id"), db_path), None
        except Exception as exc:  # noqa: BLE001
            last = exc
    return None, f"no YouTube authorization available: {str(last)[:120]}"


def run_scan(db_path=None, *, token: Optional[str] = None, max_units: int = 2500, seeds: Optional[List[str]] = None) -> dict:
    out = {"ok": False, "units": 0, "channels": 0, "videos": 0, "stopped": None, "error": None, "report_id": None}
    try:
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        if token is None:
            from hypogeum.publishing.youtube_privacy import _access_token
            from hypogeum.channel_config import list_channel_profiles
            last_exc = None
            for cp in list_channel_profiles(db_path=db_path):
                try:
                    token = _access_token(getattr(cp, "channel_id"), db_path)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            if token is None:
                out["error"] = f"no YouTube authorization available: {str(last_exc)[:120]}"
                return out
        api = _Api(token, max_units)
        seeds = seeds or pick_seeds(db_path, n=max(1, max_units // 100 - 6))      # leave room for the reads after the searches
        fx = fetch(api, seeds)
        channels, videos = _rows_from_fetch(fx)
        report = analyse(channels, videos)
        report["seeds"] = seeds
        report["stopped"] = fx["stopped"]
        report["window"] = {"max_channel_age_days": MAX_CHANNEL_AGE_DAYS, "min_channel_views": MIN_CHANNEL_VIEWS, "search_window_days": SEARCH_WINDOW_DAYS}
        conn = get_connection(db_path)
        try:
            out["report_id"] = _store(conn, channels, videos, report, fx["units"])
            conn.commit()
        finally:
            conn.close()
        out.update({"ok": True, "units": fx["units"], "channels": len(channels), "videos": len(videos), "stopped": fx["stopped"]})
        log.info("market_scan_done", units=fx["units"], channels=len(channels), videos=len(videos), niches=len(report["niches"]))
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        log.error("market_scan_failed", error=out["error"])
    return out


def latest_report(db_path=None) -> Optional[dict]:
    try:
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            r = conn.execute("SELECT report_id, created_at, units_used, channels_found, report_json FROM market_reports ORDER BY created_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if not r:
            return None
        rep = json.loads(r["report_json"])
        rep.update({"report_id": r["report_id"], "created_at": r["created_at"], "units_used": r["units_used"]})
        return rep
    except Exception:  # noqa: BLE001
        return None


_RUNNING = threading.Lock()


def maybe_run_weekly(db_path=None, *, force: bool = False) -> bool:
    """Start a scan in a background thread when none ran in the last REPORT_EVERY_DAYS (and not before 07:15 UTC, just
    after Google's quota reset, so it cannot starve the day's uploads). Returns True when a scan was started."""
    now = _now()
    if not force and now.hour * 60 + now.minute < 7 * 60 + 15:
        return False
    rep = latest_report(db_path)
    if rep and not force:
        try:
            if now - datetime.fromisoformat(rep["created_at"]) < timedelta(days=REPORT_EVERY_DAYS):
                return False
        except ValueError:
            pass
    if not _RUNNING.acquire(blocking=False):
        return False

    def _go():
        try:
            run_scan(db_path)
        finally:
            _RUNNING.release()

    threading.Thread(target=_go, name="market-scan", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# feed our own channels: the conventions of the proven niche that matches them
# ---------------------------------------------------------------------------

DIRECTIVE_DIMENSION = "market_playbook"


def _match_niche(report: dict, brief: dict) -> Optional[dict]:
    words = set()
    for x in [brief.get("genre") or ""] + list(brief.get("content_pillars") or []) + [brief.get("format") or ""]:
        words |= {w[:5] for w in re.findall(r"[a-z]+", str(x).lower()) if w not in _STOP and len(w) > 3}
    best, score = None, 0
    for n in report.get("niches", []):
        if n["channels"] < 3:
            continue
        sw = {w[:5] for w in re.findall(r"[a-z]+", n["niche"].lower()) if w not in _STOP and len(w) > 3}
        sc = len(words & sw)
        if sc > score:
            best, score = n, sc
    return best


def market_directive_text(n: dict) -> str:
    t, d = n["title"], n["description"]
    return (f"MEASURED ON NEW YOUTUBE CHANNELS IN THIS NICHE ({n['channels']} channels created in the last year, each with millions of views; "
            f"niche: {n['niche']}). Their titles run ~{t['avg_len']} characters ({t['number_pct']}% contain a number, {t['question_pct']}% are questions, "
            f"{t['power_word_pct']}% use a power word such as secret/mystery/lost/hidden); they open with: {', '.join(w for w, _ in t['top_openers'][:4])}. "
            f"Descriptions are ~{d['avg_len']} characters and {d['with_hashtags_pct']}% carry hashtags (avg {d['avg_hashtags']}). "
            f"Most used hashtags: {', '.join(h for h, _ in n['hashtags'][:8])}. They post {n['uploads_per_week']} times a week, "
            f"{round(n['shorts_share'] * 100)}% of it Shorts. Use these conventions where they fit the episode truthfully; do not copy titles.")


def learn_channel_market(channel_id: str, *, db_path=None) -> dict:
    out = {"channel_id": channel_id, "niche": None, "directive": None}
    try:
        from hypogeum.channel_creative_brief import channel_brief_for
        from hypogeum.channel_learning import get_active_directives, record_directive
        rep = latest_report(db_path)
        if not rep:
            return out
        n = _match_niche(rep, channel_brief_for(channel_id) or {})
        if not n:
            return out
        out["niche"] = n["niche"]
        text = market_directive_text(n)
        active = get_active_directives(channel_id, dimension=DIRECTIVE_DIMENSION, db_path=db_path)
        if not active or active[0]["directive_text"] != text:
            record_directive(channel_id, DIRECTIVE_DIMENSION, "prefer", text, db_path=db_path)
            out["directive"] = "written"
    except Exception as exc:  # noqa: BLE001
        log.error("market_directive_failed", channel_id=channel_id, error=str(exc)[:200])
    return out
