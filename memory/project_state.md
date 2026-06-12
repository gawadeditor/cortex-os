---
name: Project State
description: hypogeum-brain pipeline completion status, active milestone, and key decisions made during implementation
type: project
---

## Pipeline Status — 2026-06-04

| Milestone | Status | Detail |
|---|---|---|
| M0 — Foundation | **Complete** | 483 tests. config, logger, openrouter, schemas, agent, startup, pipeline scaffold. |
| M1 — Text Pipeline | **Complete** | perplexity.py + all 4 agents (EP, RD, NA, SE). System prompts in `hypogeum/prompts/`. |
| M2 — Visual Pipeline | **Complete** | muapi.py + VPD + Thumbnail Director. Path A confirmed. |
| M3 — Audio Pipeline | **Complete** | elevenlabs.py (narration) + Audio Agent. SRT generation. |
| M4.1 — ElevenLabs Music | **Complete** | Music functions in elevenlabs.py. Library-first gate, PDR on failure. 1270 tests total. |
| M4.2–M4.8 — Assembly Agent | **Complete** | assembly_agent.py full implementation (sections A–G). Music API corrections C1–C9 applied. Pipeline integration complete. 1322 tests total. |
| M5.1 — Publishing Architecture Verification | **Complete** | Plan in `docs/m5-implementation-plan.md`. YouTube = primary success, TikTok = conditional, Instagram staging = StagingProvider abstraction. |
| M5.2 — Publishing Agent implementation | **Complete** | `hypogeum/publishing/` package + `publishing_agent.py`. 1491 tests total. |
| M5.3 — Pipeline Integration Verification | **Complete** | Source attribution, chapter timestamps, SRT upload verified. 1522 tests total. |
| M5.4 — Production Readiness Audit | **Complete** | `docs/production-readiness.md` + `docs/first-production-run.md`. Readiness: ~35%. 25 operator steps remaining (all credentials + infra). No code gaps. |

**Why:** Tracking so future sessions resume at the correct milestone without re-reading all code.
**How to apply:** When starting work, read this file first to know what's done and what's next.

---

## Active Decision: MUAPI Path A Confirmed

**Fact:** MUAPI supports both image generation and image-to-video conditioning natively. Path A is in effect.

**Why:** Verified 2026-06-02 via live OpenAPI + playground schema inspection. No separate image provider needed.

**How to apply:** M2.2 reference image path implementation = Path A. Use `flux-dev-image` for reference image generation, `veo3-image-to-video` with `images_list` param for conditioned video. Ref images must be uploaded to MUAPI storage first (`POST /api/v1/upload_file`) to get a hosted URL.

---

## Active Decision: MUAPI Duration Constraint

**Fact:** MUAPI does not support free-form `duration_seconds`. Veo3 generates fixed 8s clips. Runway accepts 5 or 8 (integer enum only). No clips shorter than 5s or longer than 8s are possible with current models.

**Why:** Confirmed from live playground schema inspection.

**How to apply:** VPD scene planning (M2.3) must target 5s or 8s durations. The frozen spec's 8–15s main / 3–6s Shorts range is not fully achievable. Implementation in muapi.py maps requested duration to nearest supported value (anything ≤5 → 5, anything >5 → 8) and logs where a rounding occurred.

---

## Active Decision: ElevenLabs Music API Correction (verified 2026-06-03)

**Fact:** ElevenLabs music endpoint is `POST /v1/music` (NOT `/v1/sound-generation`). Request uses `prompt` (not `text`), `music_length_ms` in ms (not `duration_seconds`), no `prompt_influence`, add `force_instrumental: true`. Response is binary streaming, no URL.

**Why:** Verified 2026-06-03 via ElevenLabs docs + SDK analysis. `/v1/sound-generation` is sound effects only (max 30s). The spec's endpoint assumption was based on pre-music-API state.

**How to apply:** Apply 9 spec corrections (C1–C9 in `docs/elevenlabs-music-api-verification.md`) to `elevenlabs-music-integration.md` before implementing M4.4 (Assembly Agent music placement). M4.1 music functions in elevenlabs.py must also be updated if they used the old endpoint. Full details: `docs/elevenlabs-music-api-verification.md`.

---

## Key Contacts / Non-obvious Facts

- `ELEVENLABS_VOICE_ID` must be pinned before production run — irreversible channel identity decision
- YouTube OAuth requires interactive browser authorization before M5 (out-of-band step)
- Meta access token has 60-day expiry — set calendar reminder at day 55
- TikTok API availability uncertain — graceful failure expected and handled
- First episode Music Library is cold (all cache misses) — correct behavior per architecture
- MUAPI API key env var header name is `x-api-key` (NOT `Authorization: Bearer`)
- MUAPI sandbox keys available (`"is_test": true`) — use these during unit test integration to avoid billing
- ElevenLabs music: `ELEVENLABS_MUSIC_DEFAULT_DURATION` is in seconds; multiply × 1000 before sending as `music_length_ms`
