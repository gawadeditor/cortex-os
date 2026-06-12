# ElevenLabs Music API — Endpoint Verification Report

**Verification date:** 2026-06-03
**Milestone:** M4.1 — ElevenLabs Music Integration
**Spec version verified against:** `elevenlabs-music-integration.md` v1.1
**Status:** Complete — corrections required before implementation

---

## Summary

ElevenLabs has released a dedicated music generation API (`/v1/music`) that supersedes the `sound-generation` endpoint assumed in the spec. Three spec corrections are required. Authentication and asset storage architecture remain valid unchanged.

---

## 1. Endpoint Path

**Spec assumption (§2, §5.D):** `POST /v1/sound-generation`

**Verified reality:** `/v1/sound-generation` is a **sound effects** endpoint (max 30 seconds, optimized for short SFX, not music). A dedicated music API now exists:

| Endpoint | Method | Use |
|---|---|---|
| `/v1/music` | POST | Standard music generation — returns full audio (binary stream) |
| `/v1/music/stream` | POST | Low-latency streaming (same generation, chunked delivery) |
| `/v1/music/detailed` | POST | Multipart response with composition metadata |

**Correction:** Use `POST /v1/music` (not `/v1/sound-generation`). The `stream` endpoint is an alternative if streaming delivery is preferred; for the current implementation, the standard `/v1/music` endpoint returns complete audio and is appropriate.

---

## 2. Request Schema

**Spec assumption (§5.D):**
```json
{
  "text": "...",
  "duration_seconds": 120,
  "prompt_influence": 0.3,
  "model_id": "..."
}
```

**Verified reality:**

| Field | Spec | Actual | Action |
|---|---|---|---|
| Prompt field | `text` | `prompt` | **Rename to `prompt`** |
| Duration field | `duration_seconds` (float, seconds) | `music_length_ms` (integer, milliseconds) | **Rename and convert: `music_length_ms = duration_seconds * 1000`** |
| Duration range | Unspecified | 3,000ms – 600,000ms (3s – 10min) | Cap requests at 300s (300,000ms) for background tracks; omit to let model choose |
| `prompt_influence` | 0.3 | **Not a music API parameter** — specific to sound-generation | **Remove `prompt_influence` from request body** |
| `model_id` | Optional | `"music_v1"` — confirmed model identifier | Use `music_v1` as default when `ELEVENLABS_MUSIC_MODEL_ID` is unset |
| `force_instrumental` | Not in spec | Boolean; if `true`, guarantees no vocals | **Add `"force_instrumental": true`** — background underscore must be instrumental |

**Corrected request body:**
```json
{
  "prompt": "string — base category prompt, optionally extended with tone arc",
  "music_length_ms": "integer — duration_seconds × 1000; range 3000–300000",
  "model_id": "music_v1",
  "force_instrumental": true
}
```

**Note on `ELEVENLABS_MUSIC_DEFAULT_DURATION`:** The config stores this value in seconds (default: 120). Multiply by 1,000 before sending to the API. Implementation must do: `music_length_ms = config.ELEVENLABS_MUSIC_DEFAULT_DURATION * 1000`.

---

## 3. Response Format

**Spec assumption (§6.A):** Two possible formats — either binary body (`Content-Type: audio/mpeg`) or JSON with `audio_url`. Spec instructs: download from URL immediately if URL format.

**Verified reality:** The `/v1/music` endpoint always returns **binary streaming audio** (Iterator[bytes]) directly in the response body. There is no URL response format. The `audio_url` JSON branch in §6.A does not apply to this endpoint.

**Correction:**
- Remove the URL response path entirely.
- The response body IS the audio. Write the byte stream directly to disk.
- `Content-Type` will be `audio/mpeg` or the format specified in request (if an `output_format` parameter is used).

---

## 4. Duration Validation After Write

**Impact of binary streaming response on §6.A validation step 2:**

The spec checks `abs(actual_duration - requested_duration) <= 30 seconds`. Since the response is a raw binary stream (no JSON metadata containing `duration_seconds`), `actual_duration` cannot be read from the response.

**Implementation options (choose one at implementation time):**
- Option A: Skip duration validation entirely; validate only non-empty audio (byte length > 0). This is the lowest-risk option for V1.
- Option B: Read audio file duration from the written MP3 using `mutagen` or `tinytag` (adds a dependency). Aligns with the spec's validation intent.
- Option C: Treat the entire byte length as a proxy for duration (not duration in seconds, but guards against silent/empty generation similar to §6.B's byte length check).

**Recommendation for V1:** Option A — validate byte length only (non-empty). The music endpoint is purpose-built for music generation at specified durations; catastrophically short output is unlikely and can be addressed in production monitoring.

---

## 5. What Remains Valid

These sections of `elevenlabs-music-integration.md` v1.1 require **no changes**:

| Section | Topic | Status |
|---|---|---|
| §1 | Purpose and library-first model | Valid |
| §3 | Authentication — `xi-api-key` header | Valid — confirmed same header |
| §4 | Environment variables | Valid — same vars; note ms conversion for `ELEVENLABS_MUSIC_DEFAULT_DURATION` |
| §5.A | Library search gate | Valid |
| §5.B | Ancient Civilizations category | Valid |
| §5.C | Prompt construction — all 6 category prompts | Valid — content unchanged; field name changes to `prompt` (§2 correction) |
| §6.B | Ancient Civilizations elevated confidence check | Valid — byte length check still applies; duration check requires Option A/B/C selection |
| §7 | Error handling — HTTP status codes and behavior | Valid |
| §8 | Retry policy | Valid |
| §9 | Rate limit strategy | Valid |
| §10 | Asset storage paths, `music-library.json` schema | Valid |
| §11 | Logging requirements | Valid |
| §12 | Health check (first call = health check) | Valid |
| §13 | Fallback behavior (PDR on failure) | Valid |
| §14 | Cost considerations | Valid |
| §15 | Security | Valid |

---

## 6. Required Spec Updates (to be applied to v1.2)

| # | Section | Change |
|---|---|---|
| C1 | §2, §5.D | Endpoint path: `/v1/sound-generation` → `POST /v1/music` |
| C2 | §5.D | Request field: `text` → `prompt` |
| C3 | §5.D | Duration field: `duration_seconds` → `music_length_ms` (seconds × 1000); cap at 300,000 |
| C4 | §5.D | Remove `prompt_influence` (not a music API parameter) |
| C5 | §5.D | Add `"force_instrumental": true` to request body |
| C6 | §5.D | Confirm `model_id: "music_v1"` as the default |
| C7 | §6.A | Remove URL response branch; response is always binary streaming |
| C8 | §6.A | Clarify duration validation post-write; document chosen option (A/B/C) |
| C9 | §4, note | Add: `ELEVENLABS_MUSIC_DEFAULT_DURATION` is in seconds; multiply × 1000 for `music_length_ms` |

**Spec update scope:** These are corrections within the existing spec structure per the "Endpoint Verification Notice" already present in v1.1. All other sections (library model, fallback logic, asset storage, prompts) are unaffected.

---

## 7. Verification Sources

- ElevenLabs Music API docs: `https://elevenlabs.io/docs/api-reference/music/compose`
- ElevenLabs Music API docs (detailed): `https://elevenlabs.io/docs/api-reference/music/compose-detailed`
- ElevenLabs Music API docs (stream): `https://elevenlabs.io/docs/api-reference/music/stream`
- ElevenLabs Python SDK source analysis: `https://deepwiki.com/elevenlabs/elevenlabs-python/7.1-music-generation`
- ElevenLabs Music overview: `https://elevenlabs.io/docs/capabilities/music`
- ElevenLabs Sound Effects docs (for comparison): `https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert`
- Eleven Music blog announcement: `https://elevenlabs.io/blog/eleven-music-now-available-in-the-api`
