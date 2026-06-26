---
agent_name: publishing_agent
domain: distribution
implementation_file: hypogeum/agents/publishing_agent.py
status: implemented
milestone: M5.2
pipeline_stage: "9 of 9 — final stage in episode pipeline"
skills_attached: [publishing-preflight]
---

# Publishing Agent

Orchestrates distribution of `final_render_package` to YouTube, Facebook,
Instagram, and TikTok (conditional). No LLM calls — pure platform API
orchestration.

## Role

Sole authority for all platform distribution decisions. Owns:
- Pre-flight validation (disclosure text, asset files, OAuth token refresh)
- YouTube main episode upload + captions + thumbnail + end screen
- YouTube Shorts (3) upload + thumbnails
- Facebook Video upload (main) + thumbnail
- Instagram Reels (3) → stage → container → poll → publish → cleanup
- TikTok Shorts (3) upload (conditional on credentials + gate check)
- Publishing record assembly (overall_status, per-platform outcomes)
- PDR issuance on YouTube main failure or > 3 total failures
- `covered_topics_writer.record_published_topic()` after YouTube main succeeds

## Public API

```python
run_publishing_agent(
    final_render_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Path | None = None,
    youtube_privacy: str = "public",  # "public" | "private" | "unlisted"
) -> dict  # publishing_record (schema-validated)
```

`youtube_privacy="private"` is used for Episode Zero / validation runs.

## Pre-flight Steps (in order)

| Step | Action | Failure effect |
|---|---|---|
| 1 | Validate `ai_disclosure_text` present | Hard halt (`PublishingAgentError`) |
| 2 | Validate all asset files exist on disk | Hard halt |
| 3 | Refresh YouTube OAuth token | PDR + skip YouTube |
| 4 | Refresh Meta token | Log error + skip Meta |
| 5 | Refresh TikTok token | Log error + skip TikTok |

## LLM Usage

**No.** Pure platform API orchestration.

## Provider / API Usage

| Platform | API | Endpoint |
|---|---|---|
| YouTube | YouTube Data API v3 | `https://www.googleapis.com/youtube/v3` |
| YouTube Analytics | YouTube Analytics API | (token refresh only) |
| Facebook | Meta Graph API | `https://graph.instagram.com/v19.0` |
| Instagram Reels | Meta Graph API | Same as Facebook |
| TikTok | TikTok API v2 | `https://open.tiktokapis.com/v2/` |
| Staging (Instagram) | S3 or configured staging provider | `META_STAGING_BASE_URL` |

All platform API calls go through `publishing/youtube.py`, `publishing/meta.py`,
`publishing/tiktok.py` respectively — never direct `requests` calls here.

## DB Tables Touched

| Table | Operation | Via |
|-------|-----------|-----|
| `publishing_outcomes` | INSERT | `decision_inputs.store.record_publishing_outcome()` |

## Prompt Files

**None.**

## Output Artifacts

| File | Written to |
|------|-----------|
| `publishing_record_{episode_id}.json` | `.hypogeum-assets/episodes/{ep}/` |
| `.octogent/tentacles/editorial-pipeline/memory/covered-topics.md` | Updated on YouTube main success (via `covered_topics_writer`) |

## publishing_record Contents

- `package_metadata` — `overall_status`, `production_ready`, `published_at`
- `summary` — `youtube_main_published`, `total_published`, `total_failed`, `total_skipped`
- `results[]` — per-deliverable per-platform result with `platform`, `video_id`, `url`, `status`

## Calls / Dependencies

```
run_publishing_agent()
  ├── publishing.metadata.*  (description/caption builders — deterministic)
  ├── publishing.youtube.refresh_youtube_token()
  ├── publishing.youtube.upload_video()  + upload_thumbnail() + upload_captions()
  │     + apply_end_screen()  (main episode)
  ├── publishing.youtube.upload_video() × 3  (Shorts)
  ├── publishing.meta.refresh_meta_token()
  ├── publishing.meta.upload_facebook_video()
  ├── publishing.meta.upload_instagram_reel() × 3
  │     └── publishing.staging.get_staging_provider().upload()
  ├── publishing.tiktok.refresh_tiktok_token()
  ├── publishing.tiktok.upload_video() × 3  (conditional)
  ├── covered_topics_writer.record_published_topic()
  └── decision_inputs.store.record_publishing_outcome()  ← publishing_outcomes table
```

## Called By

| Caller | Context |
|--------|---------|
| `pipeline._run_publishing_agent()` | `hypogeum/pipeline.py` — Stage 9 |

## PDR Conditions

| Condition | PDR Severity |
|---|---|
| YouTube main episode upload fails | BLOCKING |
| Total failed deliverables > 3 (excluding TikTok skipped) | MAJOR |

## TikTok Gate

TikTok publishing is conditional. Skipped when:
- `config.tiktok_available` is False (credentials absent), or
- Token refresh fails at pre-flight step 5

This is a soft skip — overall_status is not degraded for TikTok skips.

## Skill Eligibility

`/publishing-preflight` — validates YouTube OAuth, Meta access token, and TikTok credentials before any episode run. Run before every episode invocation (not just ep_000). See `.claude/skills/publishing-preflight.md`.

## Tests

- `tests/test_publishing_agent.py` — platform mocking, pre-flight validation,
  PDR conditions, TikTok gate, overall_status computation

## Audit Rule

Do not modify `hypogeum/agents/publishing_agent.py` during audit or tentacle
registration. Read only.
