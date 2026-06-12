# First Production Run

**Milestone:** M5.4 — Final Production Readiness Audit
**Date:** 2026-06-04
**Prerequisite:** All items in `docs/production-readiness.md` are checked off.

---

## Overview

This document covers:

1. **Pre-run setup** — one-time steps required before any episode can publish
2. **First Production Run procedure** — step-by-step guide for episode 1
3. **First Rollback procedure** — how to recover if something goes wrong
4. **Post-run verification** — how to confirm the episode is live and correct

Estimated total operator time: **3–8 hours** (assuming platform accounts exist).

---

## Part 1: One-Time Setup (do once, before episode 1)

### Step 1: Install system dependencies

```bash
# macOS
brew install ffmpeg

# Ubuntu
sudo apt-get install ffmpeg

# Verify
ffmpeg -version
```

### Step 2: Provision credentials

Copy `.env.example` to `.env` and fill in all required values:

```bash
cp .env.example .env
# Edit .env — fill in all HARD-required values from docs/production-readiness.md
```

### Step 3: Pin the narrator voice (irreversible)

**COMPLETE — pinned 2026-06-04.**

Voice ID `j9jfwdrw7BRfcR43Qohk` is the production narrator. It is already set in `.env.example`. Copy it to `.env` — no further selection required.

```
ELEVENLABS_VOICE_ID=j9jfwdrw7BRfcR43Qohk
```

Do not change this value.

### Step 4: Obtain YouTube refresh token

This is a one-time interactive browser step:

```bash
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
print('YOUTUBE_REFRESH_TOKEN =', creds.refresh_token)
"
```

Copy the printed token into `.env` as `YOUTUBE_REFRESH_TOKEN`.

### Step 5: Verify Meta token is long-lived

Check your `META_ACCESS_TOKEN` expiry:

```bash
curl "https://graph.facebook.com/debug_token?input_token=$META_ACCESS_TOKEN&access_token=$META_APP_ID|$META_APP_SECRET"
```

Look for `"expires_at"` in the response. If the token is short-lived (< 60 days), exchange it:

```bash
curl "https://graph.facebook.com/oauth/access_token\
  ?grant_type=fb_exchange_token\
  &client_id=$META_APP_ID\
  &client_secret=$META_APP_SECRET\
  &fb_exchange_token=$META_ACCESS_TOKEN"
```

Copy the returned `access_token` into `META_ACCESS_TOKEN` in `.env`.

**Set a calendar reminder 55 days from today to refresh the token.**

### Step 6: Run the full test suite

```bash
cd /Users/gawadeditor/hypogeum-brain
pytest --tb=short -q
```

Expected: `1522 passed, 0 failed, 0 warnings`. Do not proceed if any test fails.

### Step 7: Verify startup health

```bash
python -c "
from hypogeum.startup import check_startup_health
import asyncio
result = asyncio.run(check_startup_health())
print('Status:', result['status'])
if result.get('warnings'):
    print('Warnings:', result['warnings'])
"
```

Expected: `Status: healthy`. A `degraded` status (TikTok unavailable) is acceptable. `unavailable` means a hard dependency is missing — stop and fix.

---

## Part 2: First Production Run Procedure

### Before you start

- [ ] All one-time setup steps (Part 1) are complete
- [ ] `.env` is populated — no placeholder values remaining
- [ ] `ffmpeg -version` succeeds
- [ ] `df -h .` shows > 50 GB free
- [ ] Test suite passes (1522/1522)
- [ ] Startup health is `healthy` or `degraded`

### Choose the first topic

Pick a topic that is:
- Historically well-documented (gives Research Director rich source material)
- Not in `docs/covered-topics.md` (check for duplicates)
- Between 8–15 minutes of content at typical narration pace (1200–2000 words)

Good first-episode characteristics: moderate complexity, strong visual potential, clear narrative arc. Avoid highly contentious or contested historical claims for episode 1 — simpler is better while validating the pipeline end-to-end.

### Run the pipeline

```bash
cd /Users/gawadeditor/hypogeum-brain
python run_pipeline.py --topic "Your chosen topic" --episode-id ep_001
```

**Do not interrupt the run unless a hard failure occurs.** The pipeline can take 45–90 minutes per episode (LLM calls, video generation, audio generation, rendering).

### Monitor the run

The pipeline logs to stdout. Key checkpoints to watch for:

| Log line | Meaning |
|---|---|
| `[EP] Topic brief generated` | M1 starting |
| `[RD] Research package complete` | Research done; sources captured |
| `[NA] Narrative package complete` | Story structure done |
| `[SE] Script package complete` | Full script ready |
| `[VPD] Visual package complete` | Scene specs ready |
| `[TD] Thumbnail package complete` | Thumbnail ready |
| `[AUDIO] Narration package complete` | All voice segments rendered |
| `[ASSEMBLY] Final render package complete` | Video rendered |
| `[PUBLISHING] YouTube upload complete` | **Primary success — episode is live** |
| `[PUBLISHING] Publishing record complete` | All platforms attempted |

### Exit codes

| Code | Meaning | Action |
|---|---|---|
| 0 | Complete | Verify episode is live (see Part 4) |
| 1 | Rejected | Executive Producer rejected topic — choose a different topic |
| 2 | PDR | Package Deficiency Report — read PDR output, fix the upstream issue |
| 3 | Startup failure | Check API keys and connectivity |
| 4 | Unhandled error | Read traceback, check logs |

### Expected duration

| Stage | Estimated time |
|---|---|
| EP → SE (text pipeline) | 8–15 minutes |
| VPD + TD (visual specs) | 3–5 minutes |
| Audio Agent (narration) | 10–20 minutes |
| Assembly Agent (render) | 15–30 minutes |
| Publishing Agent (upload) | 5–15 minutes |
| **Total** | **41–85 minutes** |

---

## Part 3: First Rollback Procedure

### If the pipeline fails before YouTube upload (exit code 2, 3, or 4)

The episode has not been published. No rollback of external systems is needed.

1. Read the error output and traceback
2. Identify which agent failed (EP, RD, NA, SE, VPD, TD, AUDIO, ASSEMBLY, PUBLISHING)
3. Fix the root cause (credentials, disk space, API key, etc.)
4. Re-run with the same `--episode-id`:
   ```bash
   python run_pipeline.py --topic "Same topic" --episode-id ep_001
   ```
   Assets from a partial run may be cached in `.hypogeum-assets/` — this is expected.

### If YouTube upload succeeded but the video has errors (wrong title, wrong description, wrong captions)

The video is live. YouTube allows editing metadata after upload.

**For title/description errors:**
1. Log into YouTube Studio
2. Find the video (it may be in "Processing" state for a few minutes after upload)
3. Click Edit → update title or description
4. Save

**For caption errors:**
1. YouTube Studio → Subtitles tab
2. Delete the uploaded SRT track
3. Upload a corrected SRT file manually

**For the video itself (wrong video file):**
1. YouTube does not allow replacing a video after upload — delete and re-upload
2. Delete from YouTube Studio (this is permanent)
3. Fix the assembly issue
4. Re-run with a new episode ID: `--episode-id ep_001_v2`

### If YouTube upload succeeded but other platforms failed

This is expected on first run. Facebook, Instagram, TikTok failures are non-fatal. The publishing record in `.hypogeum-assets/` will contain the per-platform outcome.

To retry failed platforms only (manual):
```bash
# Check the publishing record
cat .hypogeum-assets/episodes/ep_001/publishing_record.json

# Retry is manual for now — V1 does not have a partial-retry CLI flag
# Re-run the full pipeline with the same episode-id to attempt re-publishing
python run_pipeline.py --topic "Same topic" --episode-id ep_001 --skip-generation
# Note: --skip-generation flag is a future feature; for V1, re-run full pipeline
```

### If Meta access token expired during the run

1. Refresh the token (see Part 1, Step 5)
2. Update `META_ACCESS_TOKEN` in `.env`
3. Re-run the pipeline

### Emergency: delete a published video immediately

If the episode was published but must be taken down immediately (wrong content, rights issue, etc.):

1. YouTube Studio → Content → find the video → three-dot menu → Delete forever
2. Facebook: Creator Studio → Content Library → delete
3. Instagram: Open post → three-dot menu → Delete
4. TikTok: Creator Center → Content → delete

YouTube deletion is permanent and immediate. The video URL becomes a 404.

---

## Part 4: Post-Run Verification

Run these checks after the pipeline exits with code 0.

### 1. Confirm YouTube video is live

```bash
# Get the video ID from the publishing record
python -c "
import json
with open('.hypogeum-assets/episodes/ep_001/publishing_record.json') as f:
    record = json.load(f)
print('YouTube video ID:', record.get('youtube_video_id'))
print('YouTube URL:', f'https://www.youtube.com/watch?v={record.get(\"youtube_video_id\")}')
"
```

Open the URL in a browser. Verify:
- [ ] Title is correct
- [ ] Description contains: logline, chapter timestamps, Sources block, AI disclosure
- [ ] Captions are present (CC button appears in player)
- [ ] Thumbnail is correct
- [ ] Video plays correctly end-to-end

### 2. Verify chapter timestamps

In the YouTube description, the chapter block should look like:

```
0:00 Introduction
2:30 [Section title]
5:15 [Section title]
...
```

Click a chapter link in the description — it should jump to the correct point in the video.

### 3. Verify source attribution

The Sources block in the description should list the research sources with titles and URLs. Click each URL to confirm it resolves.

### 4. Verify SRT captions

Click CC in the YouTube player. Captions should:
- Match the narration text
- Be in sync with the audio
- Not have placeholder text or encoding errors

### 5. Check publishing record

```bash
cat .hypogeum-assets/episodes/ep_001/publishing_record.json
```

Expected fields: `youtube_video_id`, `youtube_shorts_ids` (3 entries), `facebook_video_id`, `instagram_media_id` (or error), `tiktok_video_id` (or graceful skip), `published_at`.

### 6. Add topic to covered-topics list

```bash
echo "ep_001: Your chosen topic — published $(date +%Y-%m-%d)" >> docs/covered-topics.md
```

---

## Estimated Time to First Video

| Phase | Duration | Notes |
|---|---|---|
| Platform account creation | 30–60 min | If accounts don't exist |
| Credential provisioning | 1–2 hours | API keys, OAuth flows |
| Voice selection (ElevenLabs) | 30–60 min | Irreversible — take your time |
| Meta app review | 1–7 days | Only if permissions not pre-approved; skip for first run if using test users |
| Instagram staging provisioning | 1–2 hours | Skip for first run — Instagram Reels not required for YouTube success |
| First pipeline run | 1–2 hours | Including monitoring and post-verification |
| **Total (accounts exist, Meta pre-approved)** | **3–6 hours** | |
| **Total (starting from scratch)** | **3–10 days** | Dominated by Meta app review latency |

**Recommended path to fastest first video:** Skip Instagram Reels staging and TikTok for episode 1. Focus on YouTube + Facebook. Both can be live in 3–6 hours of operator time once accounts exist.
