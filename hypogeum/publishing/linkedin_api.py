"""
hypogeum/publishing/linkedin_api.py — publish to a PERSONAL LinkedIn profile through LinkedIn's own API
(Share on LinkedIn + Sign In with OpenID Connect; scope w_member_social). Postiz cannot do this: its personal
provider demands Community Management scopes.

Credentials live only in Credential Manager:
  * 'linkedin_app'            JSON {"client_id", "client_secret"}      the developer app (one per factory)
  * 'linkedin_member:<cid>'   JSON {"access_token", "person_urn", "name"}   one signed-in profile per client
LinkedIn gives no refresh token: the member token lasts 60 days, then the operator signs in again
(token_status() feeds the alert that warns 7 days ahead).

A send is never repeated here: an outcome that may have gone through (timeout after the request left) raises
LinkedInAmbiguousError so the caller records 'ambiguous' instead of posting twice.
"""
from __future__ import annotations

import json
import re
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

log = get_logger(agent="linkedin_api")

PROVIDER = "linkedin"
APP_PROFILE_ID = "linkedin_app"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API = "https://api.linkedin.com"
# LinkedIn retires old API versions after about a year; a 426 NONEXISTENT_VERSION answer means bump this.
API_VERSION = "202601"
SCOPES = "openid profile w_member_social"
COMMENTARY_MAX = 3000
RENEW_WARN_DAYS = 7
_IMAGE_MIMES = ("image/jpeg", "image/png")
# Characters LinkedIn's "little text" commentary format treats as markup; every one must be escaped.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>*_~])|#(?![A-Za-z0-9_])")


class LinkedInError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class LinkedInRejectedError(LinkedInError):
    """LinkedIn answered and refused: nothing was posted."""


class LinkedInAmbiguousError(LinkedInError):
    """The request may have reached LinkedIn but no answer came back: never send it again automatically."""


@dataclass
class HttpResponse:
    status_code: int
    json_body: object = None
    headers: Optional[dict] = None


def _requests_send(method: str, url: str, *, headers=None, data=None, json_body=None, timeout: float = 60.0) -> HttpResponse:
    import requests
    try:
        r = requests.request(method, url, headers=headers, data=data, json=json_body, timeout=timeout)
    except (requests.ConnectTimeout, requests.exceptions.ConnectionError) as exc:
        raise LinkedInRejectedError(f"transport before send: {type(exc).__name__}") from exc   # never reached LinkedIn
    except requests.RequestException as exc:
        raise LinkedInAmbiguousError(f"transport: {type(exc).__name__}") from exc              # may have gone through
    try:
        body = r.json()
    except ValueError:
        body = None
    return HttpResponse(r.status_code, body, dict(r.headers))


def member_profile_id(client_id: str) -> str:
    return f"linkedin_member:{client_id}"


def escape_commentary(text: str) -> str:
    """Escape LinkedIn's markup characters so a caption is posted exactly as written (a bare '(' or '[' otherwise
    truncates or rejects the post). A '#tag' is kept so hashtags still link."""
    return _RESERVED.sub(lambda m: "\\" + m.group(0) if m.group(1) else "\\#", text or "")


# -- the developer app -----------------------------------------------------------------------------------------

def store_app_credentials(client_id: str, client_secret: str, *, db_path: Optional[Path] = None) -> None:
    # a copied value often drags punctuation along ("78ea…،" with an Arabic comma): the ID is plain letters+digits
    client_id = re.sub(r"[^A-Za-z0-9]", "", client_id or "")
    client_secret = (client_secret or "").strip().strip("،,;:؛ \u200f\u200e\"'`")
    data = json.dumps({"client_id": client_id, "client_secret": client_secret})
    if get_secret_profile(APP_PROFILE_ID, db_path=db_path) is None:
        create_secret_profile(provider=PROVIDER, label="LinkedIn developer app (Cortex Publisher)", credential_type="oauth2_token",
                              credential_data=data, scope="factory", secret_profile_id=APP_PROFILE_ID, db_path=db_path)
    else:
        update_secret_profile(APP_PROFILE_ID, credential_data=data, db_path=db_path)


def app_credentials(db_path: Optional[Path] = None) -> Optional[dict]:
    try:
        d = json.loads(resolve_credential(APP_PROFILE_ID, db_path=db_path))
    except Exception:  # noqa: BLE001 — missing/revoked/garbled: treated as "not configured"
        return None
    d["client_id"] = re.sub(r"[^A-Za-z0-9]", "", d.get("client_id") or "")       # a value saved before the clean-up existed
    d["client_secret"] = (d.get("client_secret") or "").strip().strip("،,;:؛ \u200f\u200e\"'`")
    return d if d["client_id"] and d["client_secret"] else None


# -- signing in --------------------------------------------------------------------------------------------------

def authorization_url(app: dict, redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": app["client_id"], "redirect_uri": redirect_uri,
                                       "state": state, "scope": SCOPES})


def sign_in(client_id: str, code: str, redirect_uri: str, *, db_path: Optional[Path] = None,
            send: Callable = _requests_send) -> dict:
    """Trade the one-time code for a 60-day token, look up whose profile it is and store both. Returns {name, expires_at}."""
    app = app_credentials(db_path)
    if not app:
        raise LinkedInError("the LinkedIn app is not configured (client id / secret missing)")
    resp = send("POST", TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                "client_id": app["client_id"], "client_secret": app["client_secret"]}))
    body = resp.json_body if isinstance(resp.json_body, dict) else {}
    if resp.status_code != 200 or not body.get("access_token"):
        raise LinkedInRejectedError(f"token exchange: HTTP {resp.status_code} {str(body.get('error') or '')[:60]}", status=resp.status_code)
    token = body["access_token"]
    me = send("GET", f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {token}"})
    info = me.json_body if isinstance(me.json_body, dict) else {}
    if me.status_code != 200 or not info.get("sub"):
        raise LinkedInRejectedError(f"userinfo: HTTP {me.status_code}", status=me.status_code)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in") or 5184000))).isoformat()
    data = json.dumps({"access_token": token, "person_urn": f"urn:li:person:{info['sub']}", "name": info.get("name") or ""})
    pid = member_profile_id(client_id)
    if get_secret_profile(pid, db_path=db_path) is None:
        create_secret_profile(provider=PROVIDER, label=f"LinkedIn profile {info.get('name') or info['sub']}", credential_type="oauth2_token",
                              credential_data=data, scope="factory", expires_at=expires, secret_profile_id=pid, db_path=db_path)
    else:
        update_secret_profile(pid, credential_data=data, expires_at=expires, db_path=db_path)
    log.info("linkedin_signed_in", client_id=client_id)
    return {"name": info.get("name") or "", "expires_at": expires}


def token_status(client_id: str, *, now: Optional[datetime] = None, db_path: Optional[Path] = None) -> dict:
    """{connected, name, expires_at, days_left, renew_soon}. Never returns the token."""
    prof = get_secret_profile(member_profile_id(client_id), db_path=db_path)
    if prof is None or prof.status != "active":
        return {"connected": False}
    try:
        name = json.loads(prof.credential_data).get("name") or ""
    except ValueError:
        name = ""
    days = None
    if prof.expires_at:
        days = (datetime.fromisoformat(prof.expires_at) - (now or datetime.now(timezone.utc))).total_seconds() / 86400
    return {"connected": days is None or days > 0, "name": name, "expires_at": prof.expires_at,
            "days_left": None if days is None else int(days), "renew_soon": days is not None and days <= RENEW_WARN_DAYS}


# -- publishing ----------------------------------------------------------------------------------------------------

class LinkedInPublisher:
    """One signed-in profile. `send` is injectable for tests."""

    def __init__(self, client_id: str, *, db_path: Optional[Path] = None, send: Callable = _requests_send):
        self.client_id, self.db_path, self._send = client_id, db_path, send

    def _member(self) -> dict:
        st = token_status(self.client_id, db_path=self.db_path)
        if not st.get("connected"):
            raise LinkedInRejectedError("LinkedIn is not connected (or the 60-day sign-in ran out)")
        return json.loads(resolve_credential(member_profile_id(self.client_id), db_path=self.db_path))

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "LinkedIn-Version": API_VERSION, "X-Restli-Protocol-Version": "2.0.0"}

    def upload_image(self, path: Path, mime: str) -> str:
        if mime not in _IMAGE_MIMES:
            raise LinkedInRejectedError(f"LinkedIn images must be JPEG or PNG (got {mime})")
        m = self._member()
        init = self._send("POST", f"{API}/rest/images?action=initializeUpload", headers=self._headers(m["access_token"]),
                          json_body={"initializeUploadRequest": {"owner": m["person_urn"]}})
        val = ((init.json_body or {}).get("value") or {}) if isinstance(init.json_body, dict) else {}
        if init.status_code != 200 or not val.get("uploadUrl") or not val.get("image"):
            raise LinkedInRejectedError(f"image init: HTTP {init.status_code}", status=init.status_code)
        up = self._send("PUT", val["uploadUrl"], headers={"Authorization": f"Bearer {m['access_token']}", "Content-Type": mime},
                        data=Path(path).read_bytes(), timeout=120.0)
        if up.status_code not in (200, 201):
            raise LinkedInRejectedError(f"image upload: HTTP {up.status_code}", status=up.status_code)
        return val["image"]

    def publish(self, text: str, *, image: Optional[str] = None) -> dict:
        """Post now. Returns {post_urn, url}. Raises LinkedInRejectedError (nothing posted) or LinkedInAmbiguousError."""
        m = self._member()
        body = {"author": m["person_urn"], "commentary": escape_commentary(text.strip()), "visibility": "PUBLIC",
                "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
                "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
        if image:
            body["content"] = {"media": {"id": image}}
        resp = self._send("POST", f"{API}/rest/posts", headers=self._headers(m["access_token"]), json_body=body)
        if resp.status_code in (200, 201):
            urn = (resp.headers or {}).get("x-restli-id") or (resp.headers or {}).get("X-RestLi-Id") or ""
            return {"post_urn": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else None}
        if resp.status_code >= 500 or resp.status_code == 429:
            raise LinkedInAmbiguousError(f"LinkedIn answered HTTP {resp.status_code} — check the profile before retrying", status=resp.status_code)
        msg = ""
        if isinstance(resp.json_body, dict):
            msg = str(resp.json_body.get("message") or resp.json_body.get("code") or "")[:100]
        raise LinkedInRejectedError(f"LinkedIn refused the post: HTTP {resp.status_code} {msg}", status=resp.status_code)
