"""
hypogeum/portal_setup.py — the client's first-run setup (multi-tenant plan, Phase 7 onboarding).

The portal's /welcome page shows five steps. Each step's state is COMPUTED from real data (keys in the vault, a
connected alert chat, an active grant), never from a "done" flag, so it can't drift from reality:

    1 Welcome          always done once you are here
    2 Your keys        OpenRouter, MUAPI, ElevenLabs, Perplexity -- every one connected
    3 Alerts           Telegram connected (or email on)
    4 Publishing       a Zernio/Postiz key is set (the operator connects social accounts with the client)
    5 Authorization    Cortex has signed the spending authorization for the channels (the operator does this)

    steps(workspace_id, db_path)               -> [{n, key, title, done, detail}]
    save_key(workspace_id, provider, key, db)  -> {"ok", "status": verified|saved|invalid, "message"}

Keys are stored exactly like the operator's /workspaces page does (vault-encrypted, scope=factory, owned by the
workspace) and are never shown again. A key a provider rejects is NOT stored.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

PROVIDERS = {
    "openrouter": {"label": "OpenRouter", "role": "Writes your scripts and plans each video",
                   "where": "openrouter.ai → Keys → Create key", "verifiable": True},
    "muapi": {"label": "MUAPI", "role": "Generates the video scenes",
              "where": "muapi.ai → Dashboard → API keys", "verifiable": True},
    "elevenlabs": {"label": "ElevenLabs", "role": "Records the narration",
                   "where": "elevenlabs.io → Profile → API keys", "verifiable": True},
    "perplexity": {"label": "Perplexity", "role": "Researches each topic",
                   "where": "perplexity.ai → Settings → API", "verifiable": False},
}
_ATTEMPTS: dict = {}


def _p(db_path):
    return Path(db_path) if db_path else None


def _http_get(url: str, headers: dict, timeout: int = 10) -> int:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def probe(provider: str, key: str, http_get: Optional[Callable] = None) -> str:
    """'ok' | 'invalid' | 'unknown' (could not tell: network, permissions, no cheap check)."""
    get = http_get or _http_get
    try:
        if provider == "openrouter":
            code = get("https://openrouter.ai/api/v1/auth/key", {"Authorization": f"Bearer {key}"})
        elif provider == "elevenlabs":
            code = get("https://api.elevenlabs.io/v1/user", {"xi-api-key": key})
        elif provider == "muapi":
            code = get("https://api.muapi.ai/api/v1/user/balance", {"x-api-key": key})
        else:
            return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"
    if code == 200:
        return "ok"
    if code in (401, 403):
        return "invalid"
    return "unknown"


def key_status(workspace_id: str, db_path=None) -> dict:
    from hypogeum import workspace_keys as wk  # noqa: PLC0415
    return {p: bool(wk.workspace_key(p, workspace_id, db_path=_p(db_path))) for p in PROVIDERS}


def save_key(workspace_id: str, provider: str, key: str, db_path=None, http_get: Optional[Callable] = None,
             actor: str = "") -> dict:
    if provider not in PROVIDERS:
        return {"ok": False, "status": "invalid", "message": "Unknown provider."}
    key = (key or "").strip()
    if not key or " " in key or len(key) < 16 or len(key) > 400:
        return {"ok": False, "status": "invalid", "message": "That does not look like an API key."}
    now = time.time()                                          # 10 attempts / hour / workspace, in this process
    hits = [t for t in _ATTEMPTS.get(workspace_id, []) if now - t < 3600]
    if len(hits) >= 10:
        return {"ok": False, "status": "invalid", "message": "Too many attempts. Try again in an hour."}
    _ATTEMPTS[workspace_id] = hits + [now]
    verdict = probe(provider, key, http_get)
    if verdict == "invalid":
        return {"ok": False, "status": "invalid", "message": f"{PROVIDERS[provider]['label']} rejected this key. Check it and try again."}
    from hypogeum.credential_manager import create_secret_profile, list_secret_profiles, revoke_secret_profile  # noqa: PLC0415
    from hypogeum.workspaces import resources_of  # noqa: PLC0415
    db = _p(db_path)
    owned = set(resources_of(workspace_id, "secret_profile", db_path=db))
    for prof in list_secret_profiles(provider=provider, db_path=db):
        if prof.secret_profile_id in owned and prof.status == "active":
            revoke_secret_profile(prof.secret_profile_id, db_path=db)
    create_secret_profile(provider, f"{workspace_id} {provider}", "api_key", key, scope="factory",
                          workspace_id=workspace_id, db_path=db)
    del key
    if verdict == "ok":
        return {"ok": True, "status": "verified", "message": f"{PROVIDERS[provider]['label']} connected and verified."}
    return {"ok": True, "status": "saved",
            "message": f"{PROVIDERS[provider]['label']} key saved. It will be checked the first time it is used."}


def steps(workspace_id: str, db_path=None) -> list:
    from hypogeum import alert_recipients as ar, workspaces as ws  # noqa: PLC0415
    from hypogeum import workspace_keys as wk  # noqa: PLC0415
    from hypogeum.client_grants import grant_state  # noqa: PLC0415
    from hypogeum import portal_auth as pa  # noqa: PLC0415
    db = _p(db_path)
    ks = key_status(workspace_id, db)
    missing = [PROVIDERS[p]["label"] for p, ok in ks.items() if not ok]
    try:
        chats = len(ar.recipients_for(workspace_id, db))
    except Exception:  # noqa: BLE001
        chats = 0
    emails = len(pa.workspace_emails(workspace_id, db))
    pub = any(wk.workspace_key(p, workspace_id, db_path=db) for p in ("zernio", "postiz"))
    channels = ws.resources_of(workspace_id, "channel", db_path=db)
    granted = bool(channels) and all(grant_state(c, db)["active"] for c in channels)
    return [
        {"n": 1, "key": "welcome", "title": "Welcome", "done": True,
         "detail": "Your portal is ready. Four short steps and your channels can start producing."},
        {"n": 2, "key": "keys", "title": "Connect your keys", "done": not missing,
         "detail": "All four connected." if not missing else "Still needed: " + ", ".join(missing) + "."},
        {"n": 3, "key": "alerts", "title": "Choose how we reach you", "done": bool(chats or emails),
         "detail": ("Telegram connected." if chats else "") + (" Email on." if emails else "") or "No way to reach you yet."},
        {"n": 4, "key": "publishing", "title": "Publishing accounts", "done": pub,
         "detail": "Your publishing account is connected." if pub else "We connect your social accounts with you. Tell us when you are ready."},
        {"n": 5, "key": "authorization", "title": "Spending authorization", "done": granted,
         "detail": "Cortex has authorized production for your channels." if granted
         else ("Cortex signs this once your keys and publishing are in place." if channels else "Your channels are being created.")},
    ]
