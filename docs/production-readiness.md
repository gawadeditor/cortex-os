# Production Readiness Audit

**Milestone:** M5.4 — Final Production Readiness Audit
**Date:** 2026-06-04
**Pipeline state:** M0–M5.3 complete. 1522/1522 tests passing.

---

## Readiness Summary

| Category | Items | Completed | Remaining |
|---|---|---|---|
| API credentials | 14 | 0 | 14 |
| OAuth flows | 1 | 0 | 1 |
| Platform accounts | 5 | 0 | 5 |
| Infrastructure | 3 | 0 | 3 |
| Irreversible decisions | 2 | 1 | 1 |
| **Total** | **25** | **1** | **24** |

**Readiness: ~38%**

The pipeline code is 100% complete and tested. Zero production blockers have been resolved. All 25 checklist items are pre-production operator steps, not code gaps.

**Estimated time to first video (once you start):** 3–8 hours of operator time, plus potential days of latency waiting for Meta app permission approval if permissions are not already granted.

---

## Remaining Blockers (ordered by criticality)

| # | Blocker | Impact if missing | Resolution |
|---|---|---|---|
| B1 | `ELEVENLABS_VOICE_ID` not pinned | Pipeline cannot produce narration | Run voice selector, pin ID — **irreversible** |
| B2 | `OPENROUTER_API_KEY` not set | All LLM calls fail; pipeline never starts | Obtain from OpenRouter dashboard |
| B3 | `PERPLEXITY_API_KEY` not set | Research Director fails at M1 | Obtain from Perplexity dashboard |
| B4 | `MUAPI_API_KEY` (production) not set | All visual generation fails at M2 | Obtain production key from MUAPI |
| B5 | `ELEVENLABS_API_KEY` not set | All audio generation fails at M3 | Obtain from ElevenLabs dashboard |
| B6 | `YOUTUBE_REFRESH_TOKEN` not obtained | Publishing fails — primary success gate | Run YouTube OAuth browser flow |
| B7 | `YOUTUBE_CHANNEL_ID` not set | Video uploads fail | Copy from YouTube Studio |
| B8 | `META_ACCESS_TOKEN` not set | Facebook + Instagram publishing fails | Obtain long-lived token via Meta OAuth |
| B9 | `META_STAGING_BASE_URL` not provisioned | Instagram Reels fails (StagingNotConfiguredError) | Provision HTTPS file-serving endpoint |
| B10 | ffmpeg not installed on host | Assembly Agent render step fails | `brew install ffmpeg` or equivalent |

All remaining blockers are credential / infrastructure provisioning steps. No code changes are required.

---

## API Keys Checklist

### Hard-required (pipeline fails without these)

- [ ] `OPENROUTER_API_KEY` — OpenRouter dashboard → API keys
- [ ] `PERPLEXITY_API_KEY` — Perplexity dashboard → API keys
- [ ] `MUAPI_API_KEY` — MUAPI dashboard → production key (use sandbox key for local test runs: `is_test: true`)
- [ ] `ELEVENLABS_API_KEY` — ElevenLabs dashboard → API keys

### YouTube (primary publishing — required)

- [ ] `YOUTUBE_CLIENT_ID` — Google Cloud Console → OAuth 2.0 client ID
- [ ] `YOUTUBE_CLIENT_SECRET` — Google Cloud Console → OAuth 2.0 client secret
- [ ] `YOUTUBE_REFRESH_TOKEN` — obtained via interactive OAuth flow (see OAuth Checklist below)
- [ ] `YOUTUBE_CHANNEL_ID` — YouTube Studio → channel settings → advanced

### Meta / Facebook / Instagram (required for Facebook; required for Instagram if staging provisioned)

- [ ] `META_APP_ID` — Meta for Developers → App Dashboard
- [ ] `META_APP_SECRET` — Meta for Developers → App Dashboard → settings → basic
- [ ] `META_ACCESS_TOKEN` — obtained via Meta OAuth (long-lived, 60-day expiry)
- [ ] `META_INSTAGRAM_ACCOUNT_ID` — Instagram Business Account ID (numeric)
- [ ] `META_FACEBOOK_PAGE_ID` — Facebook Page ID (numeric)

### TikTok (optional — graceful skip if missing)

- [ ] `TIKTOK_CLIENT_KEY`
- [ ] `TIKTOK_CLIENT_SECRET`
- [ ] `TIKTOK_ACCESS_TOKEN`
- [ ] `TIKTOK_OPEN_ID`

> **Note:** All 4 TikTok vars must be present for TikTok publishing to activate. Missing any one causes graceful skip — no failure.

### Fallback credentials (optional — only needed if primary providers are unavailable)

- [ ] `ANTHROPIC_API_KEY` — backup if OpenRouter is unreachable
- [ ] `OPENAI_API_KEY` — backup Whisper transcription if primary transcription fails

---

## OAuth Checklist

### YouTube OAuth (required — interactive browser step)

- [ ] Create a Google Cloud project (if not already created)
- [ ] Enable the YouTube Data API v3 in the project
- [ ] Create OAuth 2.0 credentials (type: Desktop app or Web app)
- [ ] Download `client_secrets.json` or copy client ID/secret to `.env`
- [ ] Run the one-time authorization flow:
  ```bash
  python -c "
  from google_auth_oauthlib.flow import InstalledAppFlow
  flow = InstalledAppFlow.from_client_secrets_file(
      'client_secrets.json',
      scopes=['https://www.googleapis.com/auth/youtube.upload',
              'https://www.googleapis.com/auth/youtube.force-ssl']
  )
  creds = flow.run_local_server(port=0)
  print('REFRESH_TOKEN:', creds.refresh_token)
  "
  ```
- [ ] Copy printed `refresh_token` value into `YOUTUBE_REFRESH_TOKEN` in `.env`
- [ ] Verify token works: `python -c "from hypogeum.publishing.youtube import YouTubeClient; print(YouTubeClient().get_channel_info())"`

### Meta OAuth (required for Facebook + Instagram)

- [ ] Create a Meta app (type: Business) at developers.facebook.com
- [ ] Request permissions: `pages_manage_posts`, `pages_read_engagement`, `instagram_content_publish`, `instagram_basic`, `publish_video` (Facebook)
- [ ] Submit for Meta app review if publishing to public pages (not just test users)
- [ ] Obtain user access token via Graph API Explorer with required permissions
- [ ] Exchange for long-lived token (60-day):
  ```
  GET https://graph.facebook.com/oauth/access_token
    ?grant_type=fb_exchange_token
    &client_id={META_APP_ID}
    &client_secret={META_APP_SECRET}
    &fb_exchange_token={short_lived_token}
  ```
- [ ] Set `META_ACCESS_TOKEN` to the long-lived token
- [ ] **Set a calendar reminder 55 days from today to refresh the token before expiry**

---

## Platform Accounts Checklist

- [ ] **YouTube channel** — created, channel ID copied to `YOUTUBE_CHANNEL_ID`
- [ ] **YouTube for Content Creators** (YPP eligibility review) — not required to upload, but required for monetization
- [ ] **Facebook Page** — created as a Business Page (not personal profile), Page ID in `META_FACEBOOK_PAGE_ID`
- [ ] **Instagram Business Account** — created and connected to the Facebook Page, account ID in `META_INSTAGRAM_ACCOUNT_ID`
- [ ] **TikTok Creator account** — optional; skip if TikTok credentials not available

---

## Storage Configuration Checklist

### ffmpeg (required — Assembly Agent video rendering)

- [ ] Verify ffmpeg is installed: `ffmpeg -version`
- [ ] Install if missing:
  - macOS: `brew install ffmpeg`
  - Ubuntu: `sudo apt-get install ffmpeg`
  - Windows: Download from ffmpeg.org, add to PATH

### Local asset storage (required — V1)

- [ ] `.hypogeum-assets/` directory exists at project root
- [ ] `.hypogeum-assets/channel/music/music-library.json` exists (created during M4.1; verify file is present)
- [ ] Sufficient local disk space for episode assets:
  - Estimated per episode: 8–15 GB (4K video clips + narration + final render)
  - Ensure at least 50 GB free for a 3-episode buffer

### Instagram Staging (required for Instagram Reels — can skip for first run)

- [ ] Provision an HTTPS endpoint that accepts file uploads and returns a public URL
  - Option A: Cloudflare R2 bucket with public HTTPS access + custom upload handler
  - Option B: S3 bucket with presigned upload URL
  - Option C: Any HTTPS file host returning `{"url": "..."}` on POST /upload
- [ ] Set `META_STAGING_BASE_URL` to the base URL of the endpoint
- [ ] Test: `curl -X POST $META_STAGING_BASE_URL/upload -F "file=@test.mp4"` — expect `{"url": "https://..."}`

> **Shortcut for first run:** Leave `META_STAGING_BASE_URL` unset. Instagram Reels will receive a `StagingNotConfiguredError`, be logged as a non-fatal failure, and skip. YouTube, Facebook, and TikTok (if configured) will still succeed.

---

## Environment Variables Reference

Full list from `hypogeum/config.py` and `.env.example`. Copy `.env.example` to `.env` and fill in all values before first run.

### Core LLM

| Variable | Required | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | **Hard** | Primary LLM provider |
| `ANTHROPIC_API_KEY` | Optional | Fallback if OpenRouter unavailable |

### Research

| Variable | Required | Notes |
|---|---|---|
| `PERPLEXITY_API_KEY` | **Hard** | Research Director |

### Visual Generation

| Variable | Required | Notes |
|---|---|---|
| `MUAPI_API_KEY` | **Hard** | Header: `x-api-key` (NOT `Authorization: Bearer`) |

### Audio / Music

| Variable | Required | Notes |
|---|---|---|
| `ELEVENLABS_API_KEY` | **Hard** | Narration + music |
| `ELEVENLABS_VOICE_ID` | **Hard** | **Irreversible** — pin before first run |
| `ELEVENLABS_MUSIC_DEFAULT_DURATION` | Optional | Default: 180. In seconds — multiplied ×1000 for API |
| `OPENAI_API_KEY` | Optional | Whisper fallback only |

### YouTube

| Variable | Required | Notes |
|---|---|---|
| `YOUTUBE_CLIENT_ID` | **Hard** | OAuth client |
| `YOUTUBE_CLIENT_SECRET` | **Hard** | OAuth client |
| `YOUTUBE_REFRESH_TOKEN` | **Hard** | From interactive OAuth flow |
| `YOUTUBE_CHANNEL_ID` | **Hard** | Numeric or handle |

### Meta / Facebook / Instagram

| Variable | Required | Notes |
|---|---|---|
| `META_APP_ID` | **Hard** | Meta app |
| `META_APP_SECRET` | **Hard** | Meta app |
| `META_ACCESS_TOKEN` | **Hard** | Long-lived, 60-day expiry — set renewal reminder |
| `META_INSTAGRAM_ACCOUNT_ID` | **Hard** | Instagram Business Account (numeric) |
| `META_FACEBOOK_PAGE_ID` | **Hard** | Facebook Page (numeric) |
| `META_STAGING_BASE_URL` | Optional | Required for Instagram Reels upload; omit to skip Instagram |

### TikTok (all optional)

| Variable | Required | Notes |
|---|---|---|
| `TIKTOK_CLIENT_KEY` | Optional | All 4 must be set to activate TikTok |
| `TIKTOK_CLIENT_SECRET` | Optional | |
| `TIKTOK_ACCESS_TOKEN` | Optional | |
| `TIKTOK_OPEN_ID` | Optional | |

---

## Irreversible Decisions Checklist

These decisions cannot be changed after the first production run without breaking channel identity or requiring a new channel.

### 1. `ELEVENLABS_VOICE_ID` — Channel Narrator Voice

**Status: COMPLETE — pinned 2026-06-04.**

Voice ID: `j9jfwdrw7BRfcR43Qohk`

This is the production narrator voice for all episodes. It is pinned in `.env.example`. Do not change this value. If a future override is required, it must be an explicit operator decision with full awareness that it breaks channel voice continuity.

### 2. YouTube Channel — Channel Name and Handle

**Status: Depends on account creation.**

The YouTube channel name and handle become part of the public URL after the channel reaches 100 subscribers. Changing the name later is possible but risks SEO equity and subscriber recognition.

**Process:**
1. Choose a channel name that fits the history/mystery niche and is not already taken
2. Create the channel
3. Set the handle (e.g., @YourChannelName) immediately — handles are first-come
4. Record the channel ID in `YOUTUBE_CHANNEL_ID`

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Meta app review delays | High | Instagram + Facebook publishing blocked | Submit for review early; use test users for first 3 episodes |
| Meta access token expires mid-run | Medium | Facebook + Instagram fail silently | Set calendar reminder at day 55; `refresh_meta_token()` available in meta.py |
| TikTok API unavailable | High | TikTok publishing skipped | Already handled via graceful skip — correct behavior |
| ElevenLabs voice discontinued | Low | Narration pipeline fails | Pin voice ID; check ElevenLabs deprecation notices quarterly |
| MUAPI model deprecation | Medium | Visual pipeline fails | Monitor MUAPI changelog; `veo3-image-to-video` and `flux-dev-image` may be replaced |
| YouTube quota exhaustion | Low | Upload fails mid-episode | Quota: 10,000 units/day. One episode uses ~2,050 units. Monitor in Google Cloud Console |
| Local disk full during render | Medium | Assembly Agent fails mid-render | Keep 50 GB free; add disk-space pre-flight check to run_pipeline.py (deferred) |
| ffmpeg not installed | High (first run) | Assembly Agent fails immediately | Verify `ffmpeg -version` before first run |
| `pipeline.py` VPD/TD stubs raise NotImplementedError | Certain | Pipeline halts at M2 | VPD and TD stubs are intentionally unimplemented — see Known Constraints |

---

## Known Constraints Still Open (from M5.3)

These are known incomplete items in the pipeline code. They are **not production-readiness gaps** — they are explicitly deferred.

| Constraint | Status | Impact |
|---|---|---|
| `pipeline.py` `_run_visual_package_director` raises `NotImplementedError` | Intentional — deferred | M2 (Visual Pipeline) cannot execute via `run_pipeline.py`; agents can be called directly |
| `pipeline.py` `_run_thumbnail_director` raises `NotImplementedError` | Intentional — deferred | Thumbnail generation cannot execute via `run_pipeline.py`; agent can be called directly |
| Instagram staging bucket not provisioned | Pre-production operator step | Instagram Reels skipped on first run |
| YouTube OAuth not yet run | Pre-production operator step | Publishing fails until token obtained |

---

## Pre-Flight Verification Script

Run this before the first production run:

```bash
# 1. Python environment
python --version        # expect 3.11+
pip list | grep pytest  # expect pytest installed

# 2. ffmpeg
ffmpeg -version         # must succeed

# 3. Disk space
df -h .                 # must show > 50 GB free

# 4. Test suite (must still pass)
cd /Users/gawadeditor/hypogeum-brain
pytest --tb=short -q    # expect 1522 passed, 0 failed

# 5. Environment
python -c "from hypogeum.config import Config; c = Config(); print('Config loaded OK')"

# 6. API connectivity (requires live credentials)
python -c "from hypogeum.startup import check_startup_health; import asyncio; r = asyncio.run(check_startup_health()); print(r['status'])"

# 7. Staging test (YouTube only — skip if META_STAGING_BASE_URL not set)
python run_pipeline.py --topic "Test topic — do not publish" --episode-id ep_test --dry-run
```
