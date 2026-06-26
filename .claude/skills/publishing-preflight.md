---
name: publishing-preflight
description: "Validate all three publishing credentials (YouTube OAuth, Meta access token, TikTok access token) before any episode production run. Use before every episode run — not just ep_000. Triggers on: publishing preflight, OAuth check, token check, YouTube token, Meta token, TikTok token, credential check before episode, publishing credentials, pre-publish check."
---

# Publishing Preflight

## Purpose

Confirm all three platform publishing credentials are present and non-expired before any episode run begins. A publishing failure after a full production run (time + money spent) is avoidable. This skill prevents it.

Run this before `/ep0-run` Gate 5 and Gate 6 for ep_000, and before every subsequent episode run.

## When to Use

- Before any invocation of `run_pipeline.py` or the dashboard Launch button
- After any period of inactivity longer than 30 days (Meta token expiry window)
- After rotating or updating any credential in `.env`
- Whenever the previous publishing run produced a platform skip or auth error

## Credential Checklist

### 1. YouTube OAuth

**Expiry:** Refresh tokens do not expire unless revoked, but access tokens expire every hour. The pipeline auto-refreshes via `YOUTUBE_REFRESH_TOKEN`.

**What to check:**
- `YOUTUBE_REFRESH_TOKEN` is set in `.env`
- `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` are set
- The token belongs to the **Hypogeum channel owner account**, not a personal account

**How to verify:** Run a token refresh dry-check:
```bash
python3 -c "
from hypogeum.config import Config
from hypogeum.publishing.youtube import refresh_youtube_token
cfg = Config()
tok = refresh_youtube_token(cfg)
print('YouTube token OK:', tok[:12] + '...')
"
```

If this raises, the refresh token is invalid. Re-authorize via `/auth/youtube` in the dashboard as the Hypogeum channel owner.

**Critical:** If the token was authorized with a personal Google account (not the Hypogeum channel owner), uploads will silently go to the wrong channel. Verify the channel ID returned by the token matches `YOUTUBE_CHANNEL_ID` in `.env`.

### 2. Meta Access Token (Facebook + Instagram)

**Expiry:** 60 days from generation. Long-lived tokens expire on a fixed calendar date.

**What to check:**
- `META_ACCESS_TOKEN` is set and non-empty in `.env`
- `META_INSTAGRAM_ACCOUNT_ID` is set
- `META_FACEBOOK_PAGE_ID` is set
- `META_STAGING_BASE_URL` is set (required for Instagram Reels staging upload)

**How to verify:** Check token expiry via Meta Graph API debug:
```bash
python3 -c "
import os, requests
from hypogeum.config import Config
cfg = Config()
r = requests.get(
    'https://graph.facebook.com/debug_token',
    params={'input_token': cfg.META_ACCESS_TOKEN,
            'access_token': f'{cfg.META_APP_ID}|{cfg.META_APP_SECRET}'}
)
d = r.json().get('data', {})
import datetime
exp = d.get('expires_at')
if exp:
    print('Meta token expires:', datetime.datetime.fromtimestamp(exp))
else:
    print('Meta token debug result:', d)
"
```

If token is expired or within 7 days of expiry: generate a new long-lived token via Meta Developer Console before proceeding.

### 3. TikTok Access Token

**Expiry:** TikTok access tokens expire after ~24 hours. A refresh token extends access for ~365 days.

**What to check:**
- `TIKTOK_ACCESS_TOKEN` is set in `.env`
- `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` are set
- `TIKTOK_OPEN_ID` is set

**How to verify:** The pipeline auto-refreshes TikTok tokens at pre-flight. Check the current token is structurally valid:
```bash
python3 -c "
from hypogeum.config import Config
cfg = Config()
print('TikTok available:', cfg.tiktok_available)
print('Open ID set:', bool(cfg.TIKTOK_OPEN_ID))
"
```

If `tiktok_available=False`, TikTok publishing will be skipped (not a hard failure). If credentials are present but expired, publishing will attempt and fail with a soft error — the episode will still publish to YouTube and Meta.

## Pre-flight Verdict

After completing the three checks above, confirm:

```
Publishing Preflight
====================
YouTube OAuth:    VALID / INVALID / WRONG ACCOUNT
Meta token:       VALID / EXPIRED / MISSING
TikTok token:     VALID / EXPIRED / SKIPPED (credentials absent)

Verdict: PROCEED / HOLD
[If HOLD: which platform and what action is required]
```

## Related Skills

- `/ep0-run` — full Episode Zero checklist (includes Gates 5 and 6 which overlap with this skill)
- `/diagnostics` — provider connectivity and API key validation
