# M5 Implementation Plan — Publishing Agent

**Created:** 2026-06-03
**Spec:** `platform-publishing-integration.md` v1.1
**Verification:** `docs/elevenlabs-music-api-verification.md` (music — already complete)
**Predecessor:** M4 complete — `final_render_package` is the Publishing Agent's sole input.

---

## 1. Publishing Agent Responsibilities

The Publishing Agent has exactly one job: take the `final_render_package` and distribute it to all target platforms. It does not generate content. It does not modify content. It adapts, stages, uploads, records, and returns a `publishing_record`.

**Concrete responsibilities:**

| Responsibility | Detail |
|---|---|
| Pre-flight token refresh | YouTube OAuth + Meta token; refresh before any upload; fail-fast on YouTube auth failure |
| Final Render Package validation | Verify all asset files declared in the package exist on disk before any upload begins |
| YouTube upload (main + 3 Shorts) | Resumable protocol; 50MB chunks; thumbnail + captions (main only) + end screen |
| Facebook Video upload | Three-phase chunked protocol; 10MB chunks; thumbnail in finish phase |
| Instagram Reels upload | Stage assets → create container → poll → publish → cleanup staged files |
| TikTok upload (conditional) | Availability gate first; chunked upload; publish status poll |
| Publishing Record construction | One `platform_results[]` entry per attempt; incremental write after each deliverable |
| Failure accounting | Track failed count; issue PDR if YouTube main fails or threshold (>3) exceeded |
| `covered-topics.md` update | After successful YouTube main upload: update episode entry with `youtube_video_id`, `youtube_url`, `published_at`, `status: completed` |

**What it does NOT do:**
- Generate video, audio, thumbnail, or caption content
- Re-run any upstream agent on failure
- Modify the Final Render Package

---

## 2. Module Structure

```
hypogeum/publishing/
    __init__.py
    youtube.py          ← YouTube Data API v3 client
    meta.py             ← Meta Graph API client (Facebook + Instagram)
    tiktok.py           ← TikTok Content Posting API client (conditional)
    staging.py          ← Storage provider abstraction for Instagram staging
    metadata.py         ← Platform metadata construction (titles, descriptions, chapters)
    retry.py            ← Shared retry logic and backoff
    record.py           ← PublishingRecord builder; incremental JSON write

hypogeum/agents/publishing_agent.py     ← Agent entry point; orchestrates the above
tests/test_publishing_agent.py          ← Unit tests (all platform calls mocked)
tests/test_publishing_youtube.py        ← YouTube client unit tests
tests/test_publishing_meta.py           ← Meta client unit tests
tests/test_publishing_tiktok.py         ← TikTok client unit tests
tests/test_publishing_staging.py        ← Staging abstraction unit tests
tests/test_publishing_metadata.py       ← Metadata construction unit tests
tests/test_publishing_record.py         ← Record builder unit tests
```

---

## 3. Provider Abstraction Strategy

Each platform is a distinct client module. The Publishing Agent calls platform clients in sequence; no platform knowledge leaks into the agent. Each client exposes a narrow public interface:

```python
# youtube.py
def refresh_youtube_token(config) -> str           # returns fresh access_token
def upload_video(config, access_token, video_path, metadata) -> str  # returns video_id
def upload_thumbnail(config, access_token, video_id, thumb_path) -> bool
def upload_captions(config, access_token, video_id, srt_path) -> bool
def apply_end_screen(config, access_token, video_id, duration_seconds) -> bool
def search_prior_episode(config, access_token) -> str | None  # prior video_id

# meta.py
def refresh_meta_token(config) -> str              # returns new access_token
def upload_facebook_video(config, access_token, video_path, metadata) -> str  # video_id
def create_instagram_container(config, access_token, video_url, thumb_url, caption) -> str  # creation_id
def poll_instagram_container(config, access_token, creation_id) -> str  # terminal status
def publish_instagram_container(config, access_token, creation_id) -> str  # media_id

# tiktok.py
def check_tiktok_availability(config) -> bool
def init_tiktok_upload(config, video_path, title) -> tuple[str, str]  # publish_id, upload_url
def upload_tiktok_chunks(config, upload_url, video_path) -> None
def poll_tiktok_publish(config, publish_id) -> str  # post_id or raises

# staging.py
class StagingProvider(Protocol):
    def stage_file(self, local_path: Path, filename: str) -> str  # returns public URL
    def delete_staged_file(self, url: str) -> None

class UrlStagingProvider:     # uses META_STAGING_BASE_URL (HTTP POST → URL)
    ...

class NullStagingProvider:    # raises immediately; used when META_STAGING_BASE_URL unset
    ...
```

This isolation means:
- Platform clients can be tested entirely in isolation with mocked HTTP
- The Publishing Agent orchestration logic is testable without any real network calls
- Adding a new platform (e.g., Pinterest) requires only a new client module

---

## 4. Retry Taxonomy

Three retry tiers with distinct semantics:

| Tier | Used for | Max retries | Initial delay | Multiplier | Max delay | Jitter |
|---|---|---|---|---|---|---|
| **Video upload** | Main video body transfer (all platforms) | 1 | 10s | 3× | 120s | ±20% |
| **Thumbnail / caption / publish** | Post-upload operations | 1 | 5s (fixed) | 1× | 5s | none |
| **Poll** | Instagram container, TikTok status | N attempts at fixed interval | 15s (Instagram), 10s (TikTok) | 1× | — | none |

**Hard failures (no retry regardless of tier):** 400, 401, 403 (all platforms); `quotaExceeded` 403 (YouTube); container ERROR/EXPIRED (Instagram); publish FAILED (TikTok).

**Soft failures (retry eligible):** 429, 500, 503, network timeout.

**`Retry-After` header:** Always honored on 429 responses — used as delay floor regardless of calculated backoff.

**YouTube quota special case:** `quotaExceeded` (403) terminates all remaining YouTube uploads in the run. It is a run-level failure, not a deliverable-level failure. No retry. PDR issued.

---

## 5. Quota Management

**YouTube (primary concern):**

| Scenario | Quota Used | Within 10,000/day? |
|---|---|---|
| Nominal run (no retries) | 7,150 | Yes |
| All 4 videos retry once | ~14,000 | **No — quota exhaustion possible** |
| Main episode only succeeds | 2,050 | Yes |

**Implementation rules:**
- Track cumulative quota units consumed in the run (`_quota_used` counter in the YouTube client)
- After every YouTube API call, add known cost to counter
- On `quotaExceeded` (403): immediately halt all remaining YouTube operations, issue PDR, record remaining deliverables as failed
- Do not retry after `quotaExceeded`
- Log current quota count at `info` level after each upload

**Meta:** No hard quota concerns at episode-per-day cadence. No explicit tracking needed in V1.

**TikTok:** Conditional; rate limits tier-dependent but not a concern at 3 Shorts/episode.

---

## 6. OAuth Handling

### YouTube
- Refresh at run start: `POST https://oauth2.googleapis.com/token` with `grant_type=refresh_token`
- Use returned `access_token` for all YouTube calls in this run
- Token lifetime: 3,600 seconds — more than sufficient for one run
- On refresh failure (401/any non-2xx): YouTube unavailable; issue PDR immediately; continue other platforms
- Refresh token rotation: out of scope for V1 — use long-lived refresh token from env

### Meta
- Refresh at run start: `GET https://graph.facebook.com/oauth/access_token?grant_type=fb_exchange_token&...`
- Update `config.META_ACCESS_TOKEN` in-memory with the new token for the duration of the run
- On refresh failure: Meta (Facebook + Instagram) unavailable; log at error; continue other platforms (no PDR — Meta is non-critical)
- 60-day token lifecycle: operational concern; `memory/project_state.md` already notes day-55 reminder

### TikTok
- No pre-run token refresh in V1 — use `TIKTOK_ACCESS_TOKEN` from env directly
- Availability gate is the first TikTok operation — if it fails, all TikTok is skipped
- Token refresh implementation: deferred to post-V1 (requires application tier confirmation)

---

## 7. Failure Recovery

**Philosophy:** Every failure is recorded. Nothing halts except YouTube main episode auth failure and clip availability at Assembly. The Publishing Record is always sealed.

| Failure | Response | PDR? | Continue? |
|---|---|---|---|
| YouTube auth failure (401) | Halt all YouTube; record remaining as `failed` | Yes — critical | Other platforms |
| YouTube `quotaExceeded` (403) | Halt all remaining YouTube; record as `failed` | Yes — critical | Other platforms |
| YouTube main episode fails after retry | Record `status: failed` | Yes — critical | Other platforms + remaining Shorts |
| YouTube Short fails after retry | Record `status: failed`; count toward threshold | No | Next Short |
| YouTube thumbnail fails | Record `thumbnail_applied: false`; video still `published` | No | Next operation |
| YouTube captions fail | Record `captions_submitted: false`; video still `published` | No | Next operation |
| YouTube end screen fails | Record `end_screen_applied: false` | No | Next operation |
| Facebook Video fails after retry | Record `status: failed`; count toward threshold | No | Instagram |
| Instagram staging fails | Record `status: failed`; skip container creation | No | Next Short |
| Instagram container ERROR/EXPIRED | Record `status: failed`; count toward threshold | No | Next Short |
| Instagram auth failure (401) | Halt all remaining Instagram | No | TikTok |
| TikTok unavailable (gate) | All TikTok: `status: skipped` | No | Seal record |
| TikTok individual upload fails | Record `status: failed`; count toward threshold | No | Next Short |
| >3 `failed` deliverables (excl. TikTok skipped) | Issue PDR `publishing_failure_threshold_exceeded` | Yes | Seal record |

**Staged file cleanup:** Always attempt cleanup regardless of container outcome. Cleanup failures are logged but do not affect Publishing Record status.

---

## 8. Platform Result Recording

The Publishing Record is written **incrementally** after each deliverable attempt. This provides a recovery artifact if the Publishing Agent crashes mid-run.

**Write path:** `.hypogeum-assets/episodes/{episode_id}/publishing_record_{episode_id}.json`

**Write timing:**
1. Initialize record at run start (status: `in_progress`, empty `platform_results[]`)
2. Append each `platform_results[]` entry immediately after the deliverable attempt completes
3. Update `summary` counts after each append
4. Seal record at run end: set `overall_status`, `package_integrity.production_ready`

**`overall_status` logic:**
```
if youtube_main_published:
    overall_status = "published"
elif any platform_results has status == "published":
    overall_status = "partially_published"
else:
    overall_status = "failed"
```

**`production_ready` logic:**
```
production_ready = (overall_status == "published")
```

**`platform_results[]` entry fields:**
```json
{
  "result_id": "uuid",
  "platform": "youtube | facebook | instagram | tiktok",
  "deliverable_type": "main_episode | short",
  "short_id": "string | null",
  "status": "published | failed | skipped",
  "skipped_reason": "string | null",
  "platform_video_id": "string | null",
  "published_url": "string | null",
  "published_at": "ISO8601 | null",
  "attempts": 1 | 2,
  "metadata_applied": {
    "title": "string",
    "description_length_chars": 0,
    "tags_count": 0,
    "thumbnail_applied": true | false,
    "captions_submitted": true | false,
    "captions_skipped_reason": "string | null",
    "ai_disclosure_included": true | false,
    "end_screen_applied": true | false | null
  }
}
```

---

## 9. Publishing Record Schema Flow

```
final_render_package
  ├── episode_render.video_asset_id      → YouTube main episode upload
  ├── episode_render.caption_asset_id   → YouTube main episode captions
  ├── shorts_renders[3].video_asset_id  → YouTube Shorts + Instagram + TikTok
  ├── thumbnail_references[]            → YouTube + Facebook + Instagram thumbnails
  ├── metadata_source.episode_title     → All platform titles
  ├── metadata_source.logline           → YouTube + Facebook descriptions
  ├── metadata_source.section_titles[]  → YouTube chapter block construction
  ├── metadata_source.shorts_titles{}   → Shorts titles (YouTube + TikTok)
  ├── metadata_source.ai_disclosure_text → Appended to all descriptions (hard gate)
  └── assembly_qa_report / package_integrity → Pre-flight validation

    ↓↓↓ Publishing Agent processes ↓↓↓

publishing_record
  ├── package_metadata.overall_status   ← "published" if youtube_main_published
  ├── platform_results[7–10 entries]    ← 1 per deliverable attempted
  │     [0] youtube  main_episode
  │     [1] youtube  short (s1)
  │     [2] youtube  short (s2)
  │     [3] youtube  short (s3)
  │     [4] facebook main_episode
  │     [5] instagram short (s1)
  │     [6] instagram short (s2)
  │     [7] instagram short (s3)
  │     [8] tiktok   short (s1)  ← conditional
  │     [9] tiktok   short (s2)  ← conditional
  │     [10] tiktok  short (s3)  ← conditional
  ├── failures[]                        ← PDR references for failed critical deliverables
  ├── summary{
  │     youtube_main_published: bool,   ← THE success condition
  │     total_deliverables_attempted,
  │     total_published,
  │     total_failed,
  │     total_skipped
  │   }
  └── package_integrity.production_ready  ← true iff youtube_main_published
```

---

## 10. Confirmed Architecture Decisions

| Decision | Value |
|---|---|
| **YouTube is the primary success condition** | `overall_status: "published"` requires and only requires `youtube_main_published == true` |
| **TikTok is conditional** | Availability gate at run start; all TikTok deliverables `status: skipped` on gate failure; no PDR |
| **Instagram staging is deferred to a configurable storage provider abstraction** | `staging.py` defines `StagingProvider` protocol; `UrlStagingProvider` (HTTP POST) and `NullStagingProvider` (raises immediately) ship in V1; cloud bucket (S3/R2) is a deployment-time configuration, not a code change |
| No platform knowledge in the agent | Publishing Agent calls platform client functions; platform clients handle all HTTP |
| Incremental record write | Record is persisted after every deliverable — never lost to a crash |
| ai_disclosure_text is a hard gate | Missing disclosure halts all publishing before any upload begins |
| YouTube main episode auth failure issues PDR | Only YouTube main failure is critical at the pipeline level |

---

## 11. M5 Task Breakdown

| Task | File | Status |
|---|---|---|
| M5.1 | Architecture verification | Complete (this doc) |
| M5.2 | Publishing Agent implementation | **Next** |
| — | `hypogeum/publishing/__init__.py` | |
| — | `hypogeum/publishing/retry.py` | |
| — | `hypogeum/publishing/metadata.py` | |
| — | `hypogeum/publishing/record.py` | |
| — | `hypogeum/publishing/staging.py` | |
| — | `hypogeum/publishing/youtube.py` | |
| — | `hypogeum/publishing/meta.py` | |
| — | `hypogeum/publishing/tiktok.py` | |
| — | `hypogeum/agents/publishing_agent.py` | |
| — | Replace `_run_publishing_agent` stub in `pipeline.py` | |
| — | `tests/test_publishing_agent.py` | |
| — | `tests/test_publishing_youtube.py` | |
| — | `tests/test_publishing_meta.py` | |
| — | `tests/test_publishing_tiktok.py` | |
| — | `tests/test_publishing_staging.py` | |
| — | `tests/test_publishing_metadata.py` | |
| — | `tests/test_publishing_record.py` | |
| M5.3 | Pipeline integration verification | After M5.2 |
| M5.4 | Session checkpoint | After all M5 tasks |
