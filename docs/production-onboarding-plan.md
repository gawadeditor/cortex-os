# Production Onboarding Plan

**Date:** 2026-06-04
**Goal:** Fastest possible first real video.
**Voice:** `j9jfwdrw7BRfcR43Qohk` — pinned, do not change.

---

## Critical Path Analysis

The fastest path to a published first video is **YouTube-only**. Facebook, Instagram, and TikTok publishing are additive — their absence does not block YouTube success.

**Minimum viable publishing stack (YouTube only):**
- OpenRouter API key
- Perplexity API key
- MUAPI API key
- ElevenLabs API key + voice pinned ✓
- YouTube OAuth credentials + refresh token
- ffmpeg installed
- `.env` populated

**Everything else** (Meta app, TikTok, Instagram staging) can be done in parallel or deferred to episode 2 without blocking episode 1.

**Estimated total operator time to episode 1:** 2–4 hours.

---

## Execution Plan

Tasks are ordered by critical-path position. Items on the critical path (marked **CP**) must be completed before the first run. Items marked **P2** can be deferred to episode 2.

---

### Task 1 — Environment Variables: Core API Keys

**Priority:** CP — must complete first  
**Estimated effort:** 20–40 minutes  
**Parallel with:** Task 2 (YouTube OAuth setup can begin simultaneously)

#### Prerequisites
- Accounts exist at: OpenRouter, Perplexity, MUAPI, ElevenLabs

#### Steps

```bash
cp .env.example .env
```

Open `.env` and populate:

| Variable | Where to find it |
|---|---|
| `OPENROUTER_API_KEY` | openrouter.ai → Account → API Keys |
| `PERPLEXITY_API_KEY` | perplexity.ai → Settings → API |
| `MUAPI_API_KEY` | MUAPI dashboard → API Keys (get production key; sandbox key has `is_test: true`) |
| `ELEVENLABS_API_KEY` | elevenlabs.io → Profile → API Keys |
| `ELEVENLABS_VOICE_ID` | Already pinned: `j9jfwdrw7BRfcR43Qohk` |

#### Blocking dependencies
None — this is the first task.

#### Validation
```bash
python -c "
from hypogeum.config import Config
c = Config()
print('OpenRouter:', bool(c.openrouter_api_key))
print('Perplexity:', bool(c.perplexity_api_key))
print('MUAPI:', bool(c.muapi_api_key))
print('ElevenLabs:', bool(c.elevenlabs_api_key))
print('Voice ID:', c.elevenlabs_voice_id)
"
```
Expected: all `True`, voice ID = `j9jfwdrw7BRfcR43Qohk`.

---

### Task 2 — YouTube OAuth Setup

**Priority:** CP — required for publishing  
**Estimated effort:** 30–60 minutes  
**Parallel with:** Task 1

#### Prerequisites
- Google account with a YouTube channel created
- YouTube channel ID available (YouTube Studio → Settings → Channel → Advanced Settings)

#### Steps

**2a. Create Google Cloud project and enable API**

1. Go to console.cloud.google.com
2. Create a new project (name: `hypogeum-brain` or similar)
3. APIs & Services → Enable APIs → search "YouTube Data API v3" → Enable
4. APIs & Services → OAuth consent screen:
   - User type: External
   - App name: Hypogeum Brain
   - Scopes: add `youtube.upload` and `youtube.force-ssl`
   - Test users: add your Google account email
5. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID:
   - Application type: Desktop app
   - Download the JSON file → save as `client_secrets.json` in project root

**2b. Run the one-time token flow**

```bash
cd /Users/gawadeditor/hypogeum-brain
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    'client_secrets.json',
    scopes=[
        'https://www.googleapis.com/auth/youtube.upload',
        'https://www.googleapis.com/auth/youtube.force-ssl'
    ]
)
creds = flow.run_local_server(port=0)
print()
print('YOUTUBE_CLIENT_ID =', creds.client_id)
print('YOUTUBE_CLIENT_SECRET =', creds.client_secret)
print('YOUTUBE_REFRESH_TOKEN =', creds.refresh_token)
"
```

A browser window will open. Authorize with your Google account. Copy the three printed values into `.env`.

**2c. Set channel ID**

```
YOUTUBE_CHANNEL_ID=<your channel ID from YouTube Studio>
```

#### Blocking dependencies
- Task 1 must be complete (`.env` file must exist to write into)
- Google Cloud project must be created (5 minutes if not already done)

#### Validation
```bash
python -c "
from hypogeum.config import Config
c = Config()
print('Client ID:', bool(c.youtube_client_id))
print('Client Secret:', bool(c.youtube_client_secret))
print('Refresh Token:', bool(c.youtube_refresh_token))
print('Channel ID:', bool(c.youtube_channel_id))
"
```
Expected: all `True`.

Live connectivity check:
```bash
python -c "
from hypogeum.publishing.youtube import YouTubeClient
client = YouTubeClient()
info = client.get_channel_info()
print('Channel:', info.get('title'))
"
```

---

### Task 3 — Infrastructure: ffmpeg + Local Storage

**Priority:** CP — Assembly Agent cannot render without ffmpeg  
**Estimated effort:** 5–10 minutes  
**Parallel with:** Tasks 1 and 2

#### Prerequisites
None.

#### Steps

```bash
# macOS
brew install ffmpeg

# Ubuntu
sudo apt-get install -y ffmpeg

# Verify
ffmpeg -version
```

Verify local asset directories:
```bash
ls -la /Users/gawadeditor/hypogeum-brain/.hypogeum-assets/
ls -la /Users/gawadeditor/hypogeum-brain/.hypogeum-assets/channel/music/
# music-library.json should be present from M4.1
```

Disk space check:
```bash
df -h /Users/gawadeditor/hypogeum-brain
# Need > 50 GB free for 3-episode buffer
```

#### Blocking dependencies
None.

#### Validation
```bash
ffmpeg -version | head -1
# expect: ffmpeg version X.X.X ...

python -c "
import subprocess
result = subprocess.run(['ffmpeg', '-version'], capture_output=True)
print('ffmpeg available:', result.returncode == 0)
"
```

---

### Task 4 — Provider Credential Validation

**Priority:** CP — run after Tasks 1–3 are complete  
**Estimated effort:** 15–20 minutes  
**Depends on:** Tasks 1, 2, 3

This task confirms all credentials are valid before attempting a real pipeline run.

#### Steps

**4a. Startup health check**
```bash
cd /Users/gawadeditor/hypogeum-brain
python -c "
from hypogeum.startup import check_startup_health
import asyncio
result = asyncio.run(check_startup_health())
print('Status:', result['status'])
for w in result.get('warnings', []):
    print('  Warning:', w)
for e in result.get('errors', []):
    print('  Error:', e)
"
```
Expected: `healthy` or `degraded` (degraded = TikTok unavailable, which is fine).

**4b. OpenRouter connectivity**
```bash
python -c "
from hypogeum.openrouter import OpenRouterClient
client = OpenRouterClient()
models = client.list_models()
print('OpenRouter OK — models available:', len(models))
"
```

**4c. ElevenLabs voice validation**
```bash
python -c "
from hypogeum.elevenlabs import get_voice_info
info = get_voice_info('j9jfwdrw7BRfcR43Qohk')
print('Voice name:', info.get('name'))
print('Voice available:', info.get('available', False))
"
```

**4d. MUAPI connectivity**
```bash
python -c "
from hypogeum.muapi import MuapiClient
client = MuapiClient()
status = client.health_check()
print('MUAPI status:', status)
"
```

**4e. YouTube token validation** (from Task 2 validation above)

#### Blocking dependencies
- Tasks 1, 2, 3 complete
- All API keys populated in `.env`

#### Validation
All checks in steps 4a–4e return no errors.

---

### Task 5 — Episode Zero Preparation

**Priority:** CP — the dry-run that proves the pipeline before committing to a real topic  
**Estimated effort:** 90–120 minutes (pipeline run time)  
**Depends on:** Tasks 1–4

Episode Zero is a real pipeline run on a safe topic, with YouTube publishing set to **Private** (not Public). This confirms end-to-end function before a public episode.

#### Prerequisites
All Tasks 1–4 complete and validated.

#### Steps

**5a. Choose Episode Zero topic**

Pick a topic that is:
- Well-documented with many sources (gives Research Director rich material)
- Short and clean narrative arc (reduces risk of agent rejection)
- Not the topic you plan for episode 1 (don't waste your best topic on a test)

Suggested: a well-known historical mystery or ancient engineering feat. Examples: The Baghdad Battery, the Antikythera Mechanism, the library of Alexandria's fate.

**5b. Set YouTube upload to Private**

In `.env`, set:
```
YOUTUBE_DEFAULT_PRIVACY=private
```

This ensures the Episode Zero video is not public while you verify the output.

> **Note:** If `YOUTUBE_DEFAULT_PRIVACY` is not a supported config var, YouTube uploads in youtube.py default to `private` unless overridden. Verify by checking:
```bash
grep -n "privacy" /Users/gawadeditor/hypogeum-brain/hypogeum/publishing/youtube.py
```

**5c. Run the pipeline**
```bash
cd /Users/gawadeditor/hypogeum-brain
python run_pipeline.py --topic "Your Episode Zero topic" --episode-id ep_000
```

Monitor stdout for checkpoint log lines (see `docs/first-production-run.md` Part 2).

**5d. Post-run checks**
- [ ] Exit code 0
- [ ] Publishing record at `.hypogeum-assets/episodes/ep_000/publishing_record.json`
- [ ] YouTube video is Private and visible in YouTube Studio
- [ ] Title, description, chapters, sources, AI disclosure all correct
- [ ] Captions present and in sync
- [ ] Video plays without visual or audio artifacts
- [ ] Log at `ep_000` shows no unexpected errors

**5e. Decision gate**

| Outcome | Action |
|---|---|
| All checks pass | Proceed to episode 1 with Public visibility |
| Minor metadata issue (wrong chapter, source missing) | Fix in YouTube Studio; note the gap; proceed |
| Audio or video artifact | Investigate assembly logs; do not proceed until clean |
| Exit code 2 (PDR) | Read PDR output; fix agent issue; re-run ep_000 |
| Exit code 3 or 4 | Fix startup/runtime error; re-run ep_000 |

Do not publish episode 1 until Episode Zero exits clean.

#### Blocking dependencies
All of Tasks 1–4.

#### Validation
Exit code 0. YouTube Studio shows the Private video. All post-run checks pass.

---

### Task 6 — Meta Developer App Setup

**Priority:** P2 — does not block episode 1 (YouTube-only)  
**Estimated effort:** 2–4 hours of setup + 1–7 days Meta review latency  
**Parallel with:** Tasks 1–5 (start early because of review latency)

#### Prerequisites
- Facebook account
- Facebook Page (Business type)
- Instagram Business Account connected to the Facebook Page

#### Steps

**6a. Create Meta Developer app**
1. developers.facebook.com → My Apps → Create App
2. App type: Business
3. App name: Hypogeum Brain (or similar)
4. Add products: Facebook Login, Instagram Graph API, Video

**6b. Request permissions**

In App Review → Permissions and Features, request:
- `pages_manage_posts`
- `pages_read_engagement`
- `publish_video` (Facebook Page video)
- `instagram_content_publish`
- `instagram_basic`

> **Latency warning:** Meta app review for `instagram_content_publish` and `publish_video` on public pages can take 1–7 days. Submit immediately and run episodes 1–2 on YouTube + Facebook Page token only.

**6c. Obtain long-lived access token**

1. Meta for Developers → Graph API Explorer
2. Select your app and Facebook Page
3. Add required permissions
4. Generate User Access Token
5. Exchange for long-lived token (60-day):

```bash
curl "https://graph.facebook.com/oauth/access_token\
  ?grant_type=fb_exchange_token\
  &client_id=$META_APP_ID\
  &client_secret=$META_APP_SECRET\
  &fb_exchange_token=<short_lived_token>"
```

6. Set `META_ACCESS_TOKEN` in `.env`

**6d. Get account IDs**

```bash
# Facebook Page ID
curl "https://graph.facebook.com/me/accounts?access_token=$META_ACCESS_TOKEN"
# Copy page id → META_FACEBOOK_PAGE_ID

# Instagram Business Account ID
curl "https://graph.facebook.com/$META_FACEBOOK_PAGE_ID?fields=instagram_business_account&access_token=$META_ACCESS_TOKEN"
# Copy id → META_INSTAGRAM_ACCOUNT_ID
```

**6e. Set renewal reminder**

Set a calendar reminder 55 days from today to refresh `META_ACCESS_TOKEN`.

#### Blocking dependencies
- Facebook Page and Instagram Business Account must exist
- Meta app review (external dependency — cannot be accelerated)

#### Validation
```bash
python -c "
from hypogeum.publishing.meta import MetaClient
client = MetaClient()
# Test Facebook page access
page = client.get_page_info()
print('Facebook Page:', page.get('name'))
"
```

---

### Task 7 — Storage Configuration: Instagram Staging

**Priority:** P2 — does not block episode 1 or 2 (Instagram Reels require a public HTTPS video URL)  
**Estimated effort:** 1–2 hours  
**Depends on:** Task 6 (Meta app must be configured before Instagram staging is useful)

Instagram Reels require a publicly accessible HTTPS video URL before publishing via the Graph API. The `UrlStagingProvider` in `hypogeum/publishing/staging.py` handles this by POSTing the video to a configured endpoint and receiving back a public URL.

#### Options (choose one)

**Option A: Cloudflare R2 (recommended for V1)**
- Low cost, no egress fees for reads
- Create R2 bucket via Cloudflare dashboard
- Enable public access on the bucket
- Build a small upload handler (Worker or direct R2 API) that returns the public URL
- Set `META_STAGING_BASE_URL` to the handler endpoint

**Option B: AWS S3 with Lambda**
- Create S3 bucket with public read
- Create Lambda function that accepts POST /upload, stores to S3, returns public URL
- Set `META_STAGING_BASE_URL` to the Lambda function URL

**Option C: Any HTTPS endpoint**
- Any server that accepts `POST /upload` with a file payload and returns `{"url": "https://..."}` works
- Must be HTTPS with a valid certificate (Meta rejects non-HTTPS URLs)

#### Blocking dependencies
- Task 6 complete (Meta credentials must be set before testing staging works end-to-end)
- Cloud infrastructure provisioned (R2 or S3)

#### Validation
```bash
# Test upload endpoint
curl -X POST $META_STAGING_BASE_URL/upload \
  -F "file=@/tmp/test_clip.mp4" \
  | python -m json.tool
# Expect: {"url": "https://..."}
```

Then run a pipeline episode with Instagram credentials set and verify Instagram Reels publishes successfully.

---

## Summary Table

| Task | Priority | Effort | Parallel | Blocks |
|---|---|---|---|---|
| 1 — Core API Keys | **CP** | 20–40 min | With Task 2, 3 | Tasks 4, 5 |
| 2 — YouTube OAuth | **CP** | 30–60 min | With Task 1, 3 | Tasks 4, 5 |
| 3 — ffmpeg + Storage | **CP** | 5–10 min | With Task 1, 2 | Tasks 4, 5 |
| 4 — Credential Validation | **CP** | 15–20 min | After 1+2+3 | Task 5 |
| 5 — Episode Zero | **CP** | 90–120 min | After 4 | Episode 1 |
| 6 — Meta App Setup | **P2** | 2–4 hr + review | Parallel with 1–5 | Task 7 |
| 7 — Instagram Staging | **P2** | 1–2 hr | After Task 6 | Instagram Reels |

**Minimum path to episode 1 (YouTube only):** Tasks 1 + 2 + 3 → 4 → 5 → episode 1 run.

**Total critical path time:** ~3–4 hours.

---

## Recommended Execution Order

```
Start here (all in parallel):
  → Task 1: Populate .env with API keys       (20–40 min)
  → Task 2: Set up YouTube OAuth              (30–60 min)
  → Task 3: Install ffmpeg                    (5 min)
  → Task 6: Begin Meta app creation           (start now — review takes days)

When Tasks 1 + 2 + 3 are all done:
  → Task 4: Validate all credentials          (15–20 min)

When Task 4 passes:
  → Task 5: Run Episode Zero (private)        (90–120 min)

When Episode Zero passes:
  → Run Episode 1 (public)

After Episode 1 publishes (or in parallel):
  → Task 7: Provision Instagram staging       (1–2 hr)
```

---

## Checklist View

### Critical path (must complete before episode 1)

- [ ] `.env` file created from `.env.example`
- [ ] `OPENROUTER_API_KEY` set
- [ ] `PERPLEXITY_API_KEY` set
- [ ] `MUAPI_API_KEY` set (production key)
- [ ] `ELEVENLABS_API_KEY` set
- [ ] `ELEVENLABS_VOICE_ID=j9jfwdrw7BRfcR43Qohk` confirmed in `.env`
- [ ] Google Cloud project created
- [ ] YouTube Data API v3 enabled
- [ ] OAuth 2.0 client credentials created + `client_secrets.json` in project root
- [ ] `YOUTUBE_CLIENT_ID` set
- [ ] `YOUTUBE_CLIENT_SECRET` set
- [ ] `YOUTUBE_REFRESH_TOKEN` obtained via browser OAuth flow
- [ ] `YOUTUBE_CHANNEL_ID` set
- [ ] `ffmpeg -version` succeeds
- [ ] `.hypogeum-assets/` directory and `music-library.json` present
- [ ] Disk space > 50 GB free
- [ ] `check_startup_health()` returns `healthy` or `degraded`
- [ ] ElevenLabs voice `j9jfwdrw7BRfcR43Qohk` confirmed available
- [ ] Episode Zero runs to exit code 0
- [ ] Episode Zero YouTube video verified (title, description, chapters, captions)

### Deferred (episode 2+)

- [ ] Meta Developer app created
- [ ] `META_APP_ID`, `META_APP_SECRET` set
- [ ] `META_ACCESS_TOKEN` (long-lived) obtained
- [ ] `META_INSTAGRAM_ACCOUNT_ID` set
- [ ] `META_FACEBOOK_PAGE_ID` set
- [ ] Meta app review approved
- [ ] Meta token renewal reminder set (55 days)
- [ ] Instagram staging endpoint provisioned
- [ ] `META_STAGING_BASE_URL` set and tested
- [ ] TikTok credentials obtained (if TikTok API becomes available)
