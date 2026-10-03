"""
hypogeum/publishing/instagram_graph.py — read a Professional Instagram
account directly through the *Instagram API with Instagram Login*
(graph.instagram.com), for the Clients feature. No Facebook Page needed,
no Zernio account (~$6/account/month) needed.

Scope is read-only: profile, recent media, per-media insights, token
refresh. Publishing stays on Postiz.

Contract (Meta docs, v25.0, checked 2026-10-03):
  GET /me?fields=user_id,username,account_type,followers_count,media_count
  GET /me/media?fields=...&limit=N            (paged via paging.next)
  GET /{media-id}/insights?metric=...         FEED: views,reach,likes,comments,
        saved,shares,total_interactions,follows,profile_visits
        REELS: same + ig_reels_avg_watch_time, reels_skip_rate
        (impressions is deprecated for media after 2024-07-02 — never requested)
  GET /refresh_access_token?grant_type=ig_refresh_token   (60-day long-lived token)
Permissions: instagram_business_basic, instagram_business_manage_insights.

The access token lives only in Credential Manager (provider='instagram_graph',
secret_profile_id='instagram_graph:<ig_user_id>') and is used for one request
parameter at a time — never logged or returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

from hypogeum.credential_manager import (
    create_secret_profile,
    get_secret_profile,
    resolve_credential,
    update_secret_profile,
)
from hypogeum.logger import get_logger

log = get_logger(agent="instagram_graph")

GRAPH = "https://graph.instagram.com"
API_VERSION = "v25.0"
PROVIDER = "instagram_graph"
_MEDIA_FIELDS = ("id,caption,media_type,media_product_type,permalink,timestamp,"
                 "like_count,comments_count,thumbnail_url,media_url")
_FEED_METRICS = ("views", "reach", "likes", "comments", "saved", "shares",
                 "total_interactions", "follows", "profile_visits")
_REELS_METRICS = ("views", "reach", "likes", "comments", "saved", "shares",
                  "total_interactions", "ig_reels_avg_watch_time", "reels_skip_rate")
_REFRESH_AFTER = timedelta(days=30)   # token lives 60 days; refresh at half-life


class InstagramGraphError(RuntimeError):
    def __init__(self, message: str, *, code=None, subcode=None):
        super().__init__(message)
        self.code, self.subcode = code, subcode


class InstagramPreConversionMedia(InstagramGraphError):
    """Meta gives no insights for media published before the account was
    last converted to a Professional account (code 100, subcode 2108006).
    Every older post has the same limit — callers stop asking for older ones.
    Likes/comments counts on the media object itself stay available."""


_PRE_CONVERSION_SUBCODE = 2108006


@dataclass
class HttpResponse:
    status_code: int
    json_body: object


def _requests_get(url: str, timeout: float = 30.0) -> HttpResponse:
    import requests
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise InstagramGraphError(f"transport: {exc}") from exc
    try:
        body = r.json()
    except ValueError:
        body = None
    return HttpResponse(r.status_code, body)


def secret_profile_id_for(ig_user_id: str) -> str:
    return f"{PROVIDER}:{ig_user_id}"


class InstagramGraphClient:
    """One Instagram account. `http_get` is injectable for tests."""

    def __init__(self, ig_user_id: str, *, db_path: Optional[Path] = None,
                 http_get: Optional[Callable[[str], HttpResponse]] = None):
        self.ig_user_id = str(ig_user_id)
        self.db_path = db_path
        self._get = http_get or _requests_get

    # -- low level ---------------------------------------------------------

    def _token(self) -> str:
        token = resolve_credential(secret_profile_id_for(self.ig_user_id), db_path=self.db_path)
        if not token:
            raise InstagramGraphError(f"no active instagram_graph credential for {self.ig_user_id}")
        return token

    def _call(self, path: str, params: dict) -> dict:
        q = urlencode({**params, "access_token": self._token()})
        resp = self._get(f"{GRAPH}/{API_VERSION}/{path.lstrip('/')}?{q}")
        body = resp.json_body if isinstance(resp.json_body, dict) else {}
        if resp.status_code != 200:
            err = (body.get("error") or {}) if isinstance(body, dict) else {}
            code, subcode = err.get("code"), err.get("error_subcode")
            cls = InstagramPreConversionMedia if subcode == _PRE_CONVERSION_SUBCODE else InstagramGraphError
            # code/subcode, not the message: Meta localises the message text.
            raise cls(f"{path}: HTTP {resp.status_code} {err.get('type', '')} code={code} subcode={subcode}"[:300],
                      code=code, subcode=subcode)
        return body

    # -- API -------------------------------------------------------------------

    def profile(self) -> dict:
        return self._call("me", {"fields": "user_id,username,account_type,followers_count,media_count"})

    def recent_media(self, *, limit: int = 100, since: Optional[datetime] = None) -> list[dict]:
        """Newest first, up to `limit` items, stopping at `since` if given."""
        out: list[dict] = []
        body = self._call("me/media", {"fields": _MEDIA_FIELDS, "limit": min(limit, 100)})
        while True:
            for m in body.get("data") or []:
                ts = _parse_ts(m.get("timestamp"))
                if since and ts and ts < since:
                    return out
                out.append(m)
                if len(out) >= limit:
                    return out
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                return out
            resp = self._get(nxt)  # already carries the token Meta put in it
            if resp.status_code != 200 or not isinstance(resp.json_body, dict):
                return out
            body = resp.json_body

    def media_insights(self, media: dict) -> dict:
        """{metric_name: value}. Reels get the Reels set; everything else the
        feed set. Falls back to a minimal set if Meta rejects a metric (the
        accepted set differs by media age/type)."""
        is_reel = (media.get("media_product_type") or "").upper() == "REELS"
        wanted = _REELS_METRICS if is_reel else _FEED_METRICS
        last: Optional[Exception] = None
        for metrics in (wanted, ("views", "reach", "likes", "comments", "saved", "shares")):
            try:
                body = self._call(f"{media['id']}/insights", {"metric": ",".join(metrics)})
            except InstagramPreConversionMedia:
                raise  # no metric set will work for this post — don't retry
            except InstagramGraphError as exc:
                last = exc
                continue
            out = {}
            for row in body.get("data") or []:
                vals = row.get("values") or []
                v = vals[0].get("value") if vals else row.get("total_value", {}).get("value")
                if v is not None:
                    out[row.get("name")] = v
            return out
        raise last or InstagramGraphError("insights unavailable")

    def refresh_token_if_due(self, *, now: Optional[datetime] = None) -> bool:
        """Refresh the long-lived token once it is >= 30 days old. Returns
        True when refreshed. Never raises for a refresh failure (logged) —
        the existing token stays valid until its own 60-day expiry."""
        now = now or datetime.now(timezone.utc)
        prof = get_secret_profile(secret_profile_id_for(self.ig_user_id), db_path=self.db_path)
        if prof is None:
            raise InstagramGraphError(f"no instagram_graph credential for {self.ig_user_id}")
        issued = _parse_ts(prof.updated_at) or _parse_ts(prof.created_at)
        if issued and now - issued < _REFRESH_AFTER:
            return False
        try:
            q = urlencode({"grant_type": "ig_refresh_token", "access_token": self._token()})
            resp = self._get(f"{GRAPH}/refresh_access_token?{q}")
            body = resp.json_body if isinstance(resp.json_body, dict) else {}
            if resp.status_code != 200 or not body.get("access_token"):
                log.warn("instagram_token_refresh_failed", ig_user_id=self.ig_user_id,
                         status=resp.status_code)
                return False
            expires = (now + timedelta(seconds=int(body.get("expires_in") or 5184000))).isoformat()
            update_secret_profile(secret_profile_id_for(self.ig_user_id),
                                  credential_data=body["access_token"], expires_at=expires,
                                  db_path=self.db_path)
            log.info("instagram_token_refreshed", ig_user_id=self.ig_user_id, expires_at=expires)
            return True
        except InstagramGraphError as exc:
            log.warn("instagram_token_refresh_failed", ig_user_id=self.ig_user_id, error=str(exc)[:200])
            return False


def store_account_token(ig_user_id: str, token: str, *, username: str = "",
                        db_path: Optional[Path] = None) -> str:
    """Create or replace the credential for one account. Returns its id."""
    sid = secret_profile_id_for(ig_user_id)
    expires = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
    if get_secret_profile(sid, db_path=db_path) is None:
        create_secret_profile(provider=PROVIDER, label=f"instagram @{username or ig_user_id}",
                              credential_type="oauth2_token", credential_data=token,
                              scope="factory", expires_at=expires, secret_profile_id=sid,
                              db_path=db_path)
    else:
        update_secret_profile(sid, credential_data=token, expires_at=expires, db_path=db_path)
    return sid


def to_cortex_metrics(insights: dict, media: dict) -> dict:
    """Map Instagram metric names onto platform_analytics' keys
    (analytics_store.store_platform_metrics)."""
    likes = insights.get("likes", media.get("like_count"))
    comments = insights.get("comments", media.get("comments_count"))
    reach = insights.get("reach")
    m = {
        "views": insights.get("views"),
        "reach": reach,
        "likes": likes,
        "comments": comments,
        "saves": insights.get("saved"),
        "shares": insights.get("shares"),
        "follows": insights.get("follows"),
        "reposts": insights.get("reposts"),
        "skip_rate": insights.get("reels_skip_rate"),
    }
    avg_ms = insights.get("ig_reels_avg_watch_time")
    if avg_ms is not None:
        m["avg_watch_time_s"] = round(float(avg_ms) / 1000.0, 2)
    total = insights.get("total_interactions")
    if total is not None and reach:
        # PERCENT, like every other source (zernio_analytics_sync: 2.66 == 2.66%)
        m["engagement_rate"] = round(100.0 * float(total) / float(reach), 2)
    return {k: v for k, v in m.items() if v is not None}


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":  # Meta's +0000
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
