"""
hypogeum/publishing/linkedin_export.py — import LinkedIn's creator-analytics
Export (.xlsx) for a Clients-feature account.

A LinkedIn *personal* profile has no analytics API for ordinary apps (Postiz's
personal-profile provider is publish-only; the scopes that would read it need
the Community Management API). The profile owner can export their own
analytics from linkedin.com/analytics/creator/content -> Export. This module
reads that file.

Workbook layout (read from a real export, 2026-10-03 — not from docs):
  DISCOVERY              'Overall Performance' | 'M/D/YYYY - M/D/YYYY'
                         'Impressions' | n ; 'Members reached' | n
  ENGAGEMENT             Date | Impressions | Engagements      (daily; values
                         can be negative after LinkedIn corrections — kept raw)
  TOP POSTS              up to 50 posts, as TWO side-by-side tables under one
                         header row: [Post URL, Post Publish Date, Engagements]
                         and [Post URL, Post Publish Date, Impressions]
  FOLLOWERS              'Total followers on M/D/YYYY' | n ; Date | New followers
  AUDIENCE DEMOGRAPHICS  Top Demographics | Value | Percentage   ('< 1%' allowed)
  CONTENT DEMOGRAPHICS   same columns
All cells arrive as strings.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from hypogeum.logger import get_logger

log = get_logger(agent="linkedin_export")

_POST_ID_RE = re.compile(r"(ugcPost|share|activity)-(\d+)")
_HANDLE_RE = re.compile(r"linkedin\.com/posts/([^_/]+)_")


class LinkedInExportError(ValueError):
    pass


def _rows(ws) -> list[list]:
    return [[("" if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]


def _date(s: str) -> Optional[str]:
    """'8/27/2026' -> '2026-08-27'."""
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date().isoformat()
    except (ValueError, AttributeError):
        return None


def _int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _pct(s: str) -> Optional[float]:
    s = (s or "").strip()
    if s.startswith("<"):
        return 0.5  # LinkedIn shows "< 1%"; store a stable midpoint, keep the raw text too
    try:
        return float(s.rstrip("%"))
    except ValueError:
        return None


def post_id_from_url(url: str) -> Optional[str]:
    m = _POST_ID_RE.search(url or "")
    return f"{m.group(1)}:{m.group(2)}" if m else None


def title_from_url(url: str) -> Optional[str]:
    """The export has no post text; LinkedIn's URL slug carries the first
    words (often hashtags). 'gawadeditor_the-footage-came-from-gaza-raw-alive-share-…'
    -> 'the footage came from gaza raw alive'."""
    m = re.search(r"/posts/[^_]+_(.+?)-(?:ugcPost|share|activity)-\d+", url or "")
    return unquote(m.group(1)).replace("-", " ").strip() or None if m else None


def parse_export(path) -> dict:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise LinkedInExportError(f"not a readable .xlsx: {exc}") from exc
    sheets = {ws.title.strip().upper(): _rows(ws) for ws in wb.worksheets}
    missing = {"DISCOVERY", "ENGAGEMENT", "TOP POSTS", "FOLLOWERS"} - set(sheets)
    if missing:
        raise LinkedInExportError(f"not a LinkedIn creator-analytics export (missing sheets {sorted(missing)})")

    out: dict = {"period_start": None, "period_end": None, "impressions": None,
                 "members_reached": None, "daily": {}, "posts": {}, "followers_total": None,
                 "audience_demographics": [], "content_demographics": [], "handle": None}

    for r in sheets["DISCOVERY"]:
        key = r[0].lower() if r else ""
        if key == "overall performance" and len(r) > 1 and " - " in r[1]:
            a, b = r[1].split(" - ", 1)
            out["period_start"], out["period_end"] = _date(a), _date(b)
        elif key == "impressions":
            out["impressions"] = _int(r[1])
        elif key == "members reached":
            out["members_reached"] = _int(r[1])
    if not out["period_end"]:
        raise LinkedInExportError("DISCOVERY sheet has no 'Overall Performance' period")

    for r in sheets["ENGAGEMENT"][1:]:
        d = _date(r[0]) if r else None
        if d:
            out["daily"].setdefault(d, {})
            out["daily"][d].update(impressions=_int(r[1]), engagements=_int(r[2]))

    rows = sheets["TOP POSTS"]
    hdr = next((i for i, r in enumerate(rows) if r and r[0] == "Post URL"), None)
    if hdr is not None:
        header = rows[hdr]
        # each side-by-side table = (url col, date col, metric col)
        tables = [(i, i + 1, i + 2, header[i + 2].lower()) for i, h in enumerate(header)
                  if h == "Post URL" and i + 2 < len(header)]
        for r in rows[hdr + 1:]:
            for ucol, dcol, mcol, metric in tables:
                url = r[ucol] if ucol < len(r) else ""
                if not url.startswith("http"):
                    continue
                p = out["posts"].setdefault(url, {"url": url, "post_id": post_id_from_url(url),
                                                  "published": _date(r[dcol]), "title": title_from_url(url)})
                p[metric] = _int(r[mcol])
                out["handle"] = out["handle"] or (_HANDLE_RE.search(url).group(1) if _HANDLE_RE.search(url) else None)

    for r in sheets["FOLLOWERS"]:
        if r and r[0].lower().startswith("total followers"):
            out["followers_total"] = _int(r[1])
        d = _date(r[0]) if r else None
        if d:
            out["daily"].setdefault(d, {})["new_followers"] = _int(r[1])

    for name, key in (("AUDIENCE DEMOGRAPHICS", "audience_demographics"),
                      ("CONTENT DEMOGRAPHICS", "content_demographics")):
        for r in sheets.get(name, [])[1:]:
            if len(r) >= 3 and r[0] and r[1]:
                out[key].append({"category": r[0], "value": r[1], "pct": _pct(r[2]), "pct_raw": r[2]})

    out["daily"] = [{"date": d, **v} for d, v in sorted(out["daily"].items())]
    out["posts"] = list(out["posts"].values())
    return out


def import_linkedin_export(client_id: str, path, *, db_path: Optional[Path] = None) -> dict:
    """Parse + store. Idempotent per (client, export period end): re-importing
    the same file updates rows in place. Returns a summary dict."""
    from hypogeum import client_manager as cm
    from hypogeum.analytics_store import store_content_metrics
    from hypogeum.db import get_connection, initialize_schema

    initialize_schema(db_path)
    data = parse_export(path)
    if cm.get_client(client_id, db_path=db_path) is None:
        raise LinkedInExportError(f"no client {client_id!r}")
    handle = data["handle"] or "linkedin"
    acct = cm.link_managed_account(client_id, "linkedin", handle=handle, provider_id="linkedin_export",
                                   provider_account_id=handle, native_account_id=handle, db_path=db_path)
    stored = 0
    for p in data["posts"]:
        if not p["post_id"]:
            continue
        cid = cm.upsert_content_item(
            client_id, "linkedin", platform_post_id=p["post_id"],
            managed_account_id=acct["managed_account_id"], url=p["url"], title=p["title"],
            published_at=p["published"], source="linkedin_export", db_path=db_path,
        )
        metrics = {}
        if p.get("impressions") is not None:
            metrics["impressions"] = p["impressions"]
        if p.get("engagements") is not None and p.get("impressions"):
            # PERCENT, same unit as every other source in platform_analytics
            metrics["engagement_rate"] = round(100.0 * p["engagements"] / p["impressions"], 2)
        if metrics:
            store_content_metrics(cid, client_id, "linkedin", metrics, source="api",
                                  snapshot_date=data["period_end"], db_path=db_path)
            stored += 1

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO client_linkedin_snapshots
                 (snapshot_id, client_id, period_start, period_end, impressions, members_reached,
                  followers_total, daily_json, audience_demographics_json, content_demographics_json,
                  source_file, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(client_id, period_end) DO UPDATE SET
                 period_start=excluded.period_start, impressions=excluded.impressions,
                 members_reached=excluded.members_reached, followers_total=excluded.followers_total,
                 daily_json=excluded.daily_json,
                 audience_demographics_json=excluded.audience_demographics_json,
                 content_demographics_json=excluded.content_demographics_json,
                 source_file=excluded.source_file, imported_at=excluded.imported_at""",
            (uuid.uuid4().hex, client_id, data["period_start"], data["period_end"], data["impressions"],
             data["members_reached"], data["followers_total"], json.dumps(data["daily"]),
             json.dumps(data["audience_demographics"], ensure_ascii=False),
             json.dumps(data["content_demographics"], ensure_ascii=False), Path(path).name, now),
        )
        conn.commit()
    finally:
        conn.close()
    summary = {"client_id": client_id, "handle": handle, "period": [data["period_start"], data["period_end"]],
               "posts": len(data["posts"]), "posts_with_metrics": stored,
               "followers_total": data["followers_total"], "impressions": data["impressions"],
               "members_reached": data["members_reached"]}
    log.info("linkedin_export_imported", **{k: v for k, v in summary.items() if k != "period"})
    return summary
