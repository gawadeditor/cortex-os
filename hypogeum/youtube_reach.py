"""
hypogeum/youtube_reach.py — thumbnail impressions and click-through per YouTube video.

The YouTube Analytics API does not give impressions/CTR per video, so the "was the packaging good enough to be
clicked?" layer of hypogeum.revival_engine was blind for YouTube. The YouTube Reporting API does: report type
`channel_reach_basic_a1` (dimensions date, channel_id, video_id; metrics video_thumbnail_impressions and
video_thumbnail_impressions_ctr). It is asynchronous: a *job* is created once, YouTube then produces one report per
day (the first one up to ~48h later, with ~30 days of backfill), each downloadable as CSV.

    ensure_job(token)                    -> job id (reuses an existing job for the report type)
    parse_reach_csv(text)                -> [{day, video_id, impressions, ctr}]            (pure)
    sync_channel_reach(channel_id)       -> create job if needed, download new reports, store     (never raises)
    channel_ctr(conn, channel_id)        -> weighted CTR + per-video rows                          (pure read)

Known limit: the Shorts feed has no thumbnail to click, so a Shorts-only channel may legitimately get few or no
rows. An empty result is reported as "pending"/"empty", never as a zero CTR.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from hypogeum.logger import get_logger

log = get_logger(agent="youtube_reach")

REPORT_TYPE = "channel_reach_basic_a1"
_BASE = "https://youtubereporting.googleapis.com/v1"
_TIMEOUT = 40
_last_error = ""   # last non-200 response, so the caller can tell the operator WHY (e.g. API not enabled)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(token: str, url: str, params: Optional[dict] = None) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        log.warn("youtube_reach_network_error", error=str(exc)[:160])
        return None
    if r.status_code != 200:
        global _last_error
        try:
            _last_error = r.json().get("error", {}).get("message", "")[:300]
        except ValueError:
            _last_error = r.text[:300]
        log.warn("youtube_reach_http", status=r.status_code, body=_last_error)
        return None
    return r


def ensure_job(token: str) -> Optional[str]:
    """The id of this report type's job, creating it on first use. None on any failure."""
    r = _get(token, f"{_BASE}/jobs")
    if r is None:
        return None
    for job in r.json().get("jobs", []):
        if job.get("reportTypeId") == REPORT_TYPE:
            return job.get("id")
    try:
        c = requests.post(f"{_BASE}/jobs", headers={"Authorization": f"Bearer {token}"},
                          json={"reportTypeId": REPORT_TYPE, "name": "cortex thumbnail reach"}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        log.warn("youtube_reach_job_create_error", error=str(exc)[:160])
        return None
    if c.status_code != 200:
        log.warn("youtube_reach_job_create_http", status=c.status_code, body=c.text[:200])
        return None
    return c.json().get("id")


def parse_reach_csv(text: str) -> List[dict]:
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        try:
            day = rec["date"]
            day = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 and day.isdigit() else day
            rows.append({
                "day": day, "video_id": rec["video_id"],
                "impressions": int(float(rec["video_thumbnail_impressions"])),
                "ctr": float(rec["video_thumbnail_impressions_ctr"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _store(conn, channel_id: str, rows: List[dict]) -> int:
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO youtube_reach_daily (channel_id, day, video_id, impressions, ctr, fetched_at) "
            "VALUES (?,?,?,?,?,?)", (channel_id, r["day"], r["video_id"], r["impressions"], r["ctr"], _now()))
    return len(rows)


def sync_channel_reach(channel_id: str, *, db_path=None, token: Optional[str] = None) -> dict:
    """Create the job if needed and download any reports we do not have. Never raises.

    status: ok | pending (job exists, YouTube has produced no report yet) | no_auth | error
    """
    out = {"channel_id": channel_id, "status": "error", "rows": 0, "job_id": None, "error": None}
    try:
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        if token is None:
            try:
                from hypogeum.publishing.youtube_privacy import _access_token
                token = _access_token(channel_id, db_path)
            except Exception as exc:  # noqa: BLE001 -- no/dead authorization is a normal state
                out.update(status="no_auth", error=str(exc)[:160])
                return out
        job_id = ensure_job(token)
        if not job_id:
            out["error"] = _last_error or "could not create or find the reporting job"
            return out
        out["job_id"] = job_id
        with get_connection(db_path) as conn:
            last = conn.execute("SELECT MAX(fetched_at) FROM youtube_reach_daily WHERE channel_id=?",
                                (channel_id,)).fetchone()[0]
        params = {"createdAfter": (datetime.fromisoformat(last) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")} if last else {}
        listing = _get(token, f"{_BASE}/jobs/{job_id}/reports", params)
        if listing is None:
            out["error"] = "could not list reports"
            return out
        reports = listing.json().get("reports", [])
        if not reports and not last:
            out["status"] = "pending"
            return out
        total = 0
        for rep in reports:
            dl = rep.get("downloadUrl")
            body = _get(token, dl) if dl else None
            if body is None:
                continue
            with get_connection(db_path) as conn:
                total += _store(conn, channel_id, parse_reach_csv(body.text))
                conn.commit()
        out.update(status="ok", rows=total)
    except Exception as exc:  # noqa: BLE001 -- learning must never break the worker
        out["error"] = str(exc)[:200]
        log.error("youtube_reach_failed", channel_id=channel_id, error=out["error"])
    return out


def channel_ctr(conn, channel_id: str) -> Optional[dict]:
    """{impressions, ctr (impression-weighted), videos: [{video_id, impressions, ctr}]} or None with no data."""
    rows = conn.execute(
        "SELECT video_id, SUM(impressions), SUM(impressions * ctr) FROM youtube_reach_daily "
        "WHERE channel_id=? GROUP BY video_id", (channel_id,)).fetchall()
    vids = [{"video_id": v, "impressions": int(i or 0), "ctr": (c / i) if i else None} for v, i, c in rows]
    total = sum(v["impressions"] for v in vids)
    if not total:
        return None
    return {"impressions": total, "ctr": sum((v["ctr"] or 0) * v["impressions"] for v in vids) / total, "videos": vids}
