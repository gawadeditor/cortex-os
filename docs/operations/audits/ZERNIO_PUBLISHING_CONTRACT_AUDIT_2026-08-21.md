# Zernio Publishing Contract Audit (Media, Scheduling, Response Envelope) — 2026-08-21

Status: read-only audit artifact. No implementation, ADR, or production
code was modified as part of producing this document. No POST, PUT, PATCH,
DELETE, or GraphQL mutation was sent to Zernio. No media was uploaded. No
content was published or scheduled. No credential value was read, printed,
retrieved, or exposed anywhere in this document or during its production.

HEAD audited: `d3fe4323fc9e55545335ee47371764c3a04f9be4` (unchanged
throughout this task — no code was edited).

Purpose: resolve the three contract gaps left open by
`docs/operations/checkpoints/ADR013_ITEM4_ZERNIO_ADAPTER_CHECKPOINT_2026-08-21.md`
— real media attachment, scheduling request fields, and `POST /v1/posts`
success-response shape — using the full official OpenAPI specification
(not the earlier partial summary), without any live write.

---

## 1. Sources inspected

1. **`https://docs.zernio.com/api/openapi`** — official OpenAPI 3.1.0 spec,
   `info.title: "Zernio API"`, `info.version: "1.0.4"`. Retrieved
   2026-08-21 via one read-only `GET`. Saved locally in full (46,262 lines,
   ~2.2MB YAML) and read directly line-by-line for every claim below — the
   AI-summarized excerpt from the fetch tool's own first pass was
   **discarded** after it produced a summary that contradicted the raw
   spec on two material points (see §9, "Retracted evidence") and was
   never used as a source for any claim in this document.
2. `docs/operations/audits/ZERNIO_CONTRACT_AUDIT_2026-08-21.md` — prior
   sanitized audit (existing repository artifact), used only to identify
   what was already confirmed vs. still open.
3. `DECISION_LOG.md` ADR-013 — architecture requirement.
4. `hypogeum/publishing/zernio.py` — current adapter source, read in full.
5. `tests/test_publishing_zernio.py` — current fake-transport test
   coverage, read in full.

No SDK or frontend source was consulted — the OpenAPI spec fully answered
every Phase B question, so lower-priority sources (SDK, frontend metadata)
were not needed. **No authenticated live GET request was made in this
task** — nothing beyond the one public, unauthenticated documentation
fetch above.

## 2. Endpoint / base-path determination — VERIFIED (official documentation)

- Base URL (from `info.description`): `https://zernio.com/api`. All
  operations below are under `/v1`, confirmed by the raw path keys in the
  spec (`/v1/posts`, `/v1/media/presign`, etc.) — i.e. the full path is
  `https://zernio.com/api/v1/posts`, matching the prior audit's
  `Servers:` block finding exactly.
- Create-post operation: `operationId: createPost`, `POST /v1/posts`.
- Auth: `Authorization: Bearer <API key>` (spec `info.description`:
  "Authenticate with a Bearer API key").
- Content-Type: `application/json` (the `requestBody.content` key is
  exactly `application/json` — **not** `multipart/form-data**; media is
  never embedded as raw bytes in this call, see §3).

## 3. Media attachment — VERIFIED (official documentation, `MediaItem` schema)

**Request fields** (`POST /v1/posts` body):
- `mediaItems: MediaItem[]` — top-level, applies to every targeted
  platform.
- `platforms[].customMedia: MediaItem[]` — per-platform override, same
  `MediaItem` shape.

**`MediaItem` schema** (`components.schemas.MediaItem`):

| Field | Type | Notes |
|---|---|---|
| `type` | `enum [image, video, gif, document]` | |
| `url` | `string, format: uri` | "URLs must be publicly reachable over HTTPS." **This is the content-attachment mechanism** — a URL reference, not an uploaded file embedded in the request. |
| `title` | string | optional; used for LinkedIn PDF/carousel doc title |
| `altText` | string | optional; accessibility text, several platforms |
| `filename` | string | optional |
| `size` | integer | optional, bytes |
| `mimeType` | string | optional |
| `thumbnail` | `string, format: uri` | **Answers "thumbnail if supported": YES.** Custom cover/thumbnail URL for videos — documented as supported specifically for "Facebook video posts, Facebook Reels, regular video uploads, and LinkedIn video posts." Max 10MB, JPG/PNG recommended. |
| `instagramThumbnail` | `string, format: uri` | Instagram-Reels-specific cover override; falls back through `platformSpecificData.instagramThumbnail` → `.reelCover` → `.thumbnailUrl` (legacy) if unset. |

No field for an "uploaded media identifier" exists anywhere in the spec —
Zernio's media model is **URL-reference-only**, never an opaque ID you
attach by reference (confirmed by grepping every `MediaItem`-adjacent
schema; no `mediaId`/`assetId`/`uploadId` field exists on `MediaItem` or
in the `createPost` request body).

## 4. Media upload mechanism — VERIFIED (official documentation)

Two, and only two, ways to get a URL into `mediaItems[].url`:

1. **Bring your own publicly-reachable HTTPS URL.** No Zernio call
   required at all — any URL the post request can supply directly. This
   is the path Cortex's existing staging infrastructure
   (`hypogeum/publishing/staging.py`, already used for Instagram) already
   produces, so **no new upload step is structurally required** for
   Cortex to use this adapter for real content.
2. **`POST /v1/media/presign`** (`operationId: getMediaPresignedUrl`) —
   presigned upload, for files up to 5GB:
   - Request: `{filename: string (required), contentType: string
     (required, enum of specific image/video/audio/pdf MIME types —
     `video/mp4` is included), size?: integer}`.
   - Response (`200`): `{uploadUrl: string (uri, expires in 1h),
     publicUrl: string (uri), key: string, expiresIn: integer (always
     3600)}`.
   - Flow: caller `PUT`s the raw file to `uploadUrl` (cloud storage, not
     Zernio's API), then uses `publicUrl` as `mediaItems[].url` in the
     `createPost` call.
   - Errors: `400` (missing filename/unsupported contentType/size out of
     range), `401`.

This resolves the "multipart vs. presigned vs. separate endpoint vs. URL
reference vs. embedded" question completely: it is **URL reference**,
optionally produced by Zernio's own **presigned upload** helper. It is
**never** multipart-embedded in the `createPost` call itself, and there is
**no** separate "upload then reference by opaque media ID" flow.

## 5. Scheduling contract — VERIFIED (official documentation)

**Request fields** (`POST /v1/posts` body):

| Field | Type | Notes |
|---|---|---|
| `scheduledFor` | `string, format: date-time` | Top-level; ISO 8601, examples use a `Z`-suffixed UTC instant (e.g. `"2024-11-01T10:00:00Z"`). |
| `timezone` | `string, default: UTC` | Present alongside `scheduledFor` on every example. The spec does not separately document how `timezone` reinterprets an already-offset-bearing `scheduledFor` value — see §8, remaining unknown. |
| `publishNow` | `boolean, default: false` | Immediate-publish switch. |
| `isDraft` | `boolean, default: false` | "When none of `scheduledFor`, `publishNow`, or `queuedFromProfile` are provided, the post defaults to draft automatically." |
| `platforms[].scheduledFor` | `string, format: date-time` | **Per-platform override — answers "can each platform have a distinct scheduled time": YES**, confirmed field. "When omitted, the top-level `scheduledFor` is used." |
| `queuedFromProfile` / `queueId` | string | Queue-based auto-scheduling (next available slot); not needed for Cortex's fixed-schedule use case but confirmed present. |

**Immediate vs. scheduled behavior**: `publishNow: true` → response
includes `platformPostUrl` immediately (`status: "published"` in the
example). Scheduled (`scheduledFor` set, `publishNow` omitted/false) →
`status: "scheduled"`, `platformPostUrl` populated later, fetched via
`GET /v1/posts/{postId}` after publish time.

**Update / cancel before publication — VERIFIED**:
- `PATCH /v1/posts/{postId}` (`operationId` update): request body accepts
  `scheduledFor`, `platforms`, `isDraft` ("Send `false` to promote a draft
  to scheduled"), etc. — **a scheduled post's time can be changed**.
  Response `200` → `PostUpdateResponse = {message, post: Post, warnings?:
  string[]}`.
- `DELETE /v1/posts/{postId}` (`operationId: deletePost`): "Delete a draft
  or scheduled post from Zernio. Published posts cannot be deleted; use
  the Unpublish endpoint instead." **Confirms a scheduled (not-yet-
  published) post can be cancelled.** Response `200` →
  `PostDeleteResponse = {message}`. `400` if the post is already
  published.
- `POST /v1/posts/{postId}/unpublish` exists as a separate operation for
  already-published content (not inspected in detail — out of scope for
  "before publication").

**Recurring schedule (Cortex's stated operational goal)**: two distinct
mechanisms exist, neither of which is "fixed calendar-time recurrence" in
the RRULE sense Cortex's own `publish_calendar`/`schedules` tables encode:
- `recycling` (`RecyclingConfig`): reposts the **same already-published**
  post at a `gap`/`gapFreq` (week/month) interval after it publishes —
  "YouTube and TikTok platforms are excluded from recycling." Not
  applicable to Cortex's "new episode each cycle" model.
- `queuedFromProfile`/`queueId`: auto-assigns **new** posts to the next
  open slot in a pre-configured per-profile posting queue/calendar
  (schedule-template semantics, closer to Cortex's actual need) — its
  queue-configuration endpoints were not inspected in this pass (out of
  the six Phase B questions; flagged in §11 as worth a follow-up read).

## 6. Response envelope — VERIFIED (official documentation) — **corrects the prior audit**

`components.schemas.PostCreateResponse`:
```
{ message: string, post: Post }
```
**Not** a bare `Post` object at the top level. `Post` (full schema) —
relevant fields: `_id`, `title`, `content`, `mediaItems`, `platforms:
PlatformTarget[]`, `scheduledFor`, `timezone`, `status: enum [draft,
scheduled, publishing, published, failed, partial]`, `tags`, `hashtags`,
`visibility`, `metadata`, `recycling`, `createdAt`, `updatedAt`.

`PlatformTarget` (each entry of `post.platforms[]`):

| Field | Notes |
|---|---|
| `platform` | destination platform string |
| `accountId` | `oneOf [string, SocialAccount]` — an embedded object in responses, a bare string in requests |
| `status` | `enum`-like: "pending, publishing, published, failed" |
| `platformPostId` | **native platform post ID** — confirmed present at this nesting (matches the prior audit's finding, just at the corrected top-level path) |
| `platformPostUrl` | `string \| null`; empty for some TikTok cases until a reconcile cron backfills it |
| `publishedAt` | timestamp |
| `errorMessage` | human-readable, present when `status: "failed"` |
| `errorCategory` | **new, richer evidence not in the prior audit** — `enum [auth_expired, user_content, user_abuse, account_issue, platform_rejected, platform_error, platform_rate_limit, quota_exhausted, system_error, unknown]` |
| `errorSource` | `enum [user, platform, system]` |
| `removedFromPlatformAt` | post-publish takedown detection (analytics-sync lag) |

HTTP status for a successful `createPost`: **`201`**, not a generic `2xx`
assumption (the spec's only documented success code for this operation is
`201`).

**Idempotency — corrects the prior audit's conflation of two distinct
mechanisms:**
1. **Same-`x-request-id` retry (≤5 min window).** Returns **HTTP 200**
   (not 409) with the original post in an `existingPost` field. The prior
   audit's "409 + existingPostId" description was **conflating this with
   mechanism 2** — the two are explicitly documented as separate, and the
   spec states them in an explicit precedence order: "same-`x-request-id`
   retries (200) are checked first; if no idempotency match, the
   content-hash dedup (409) runs."
2. **Content-hash dedup (24h window), independent of `x-request-id`.**
   Hashes `(platform, accountId, content + media URLs)`; a match within
   24h → **HTTP 409** with body `{error: string, details: {accountId,
   platform, existingPostId}}` — **`existingPostId` is nested under
   `details`, not top-level.**

## 7. Failure/error behavior — VERIFIED (official documentation)

| Status | Meaning | Body shape |
|---|---|---|
| `400` | Validation error | `{error: string}` |
| `401` | Unauthorized | `$ref: components/responses/Unauthorized` (shared component, not separately re-read in this pass) |
| `403` | Forbidden, three sub-cases distinguished by an optional `code` field | `{error: string, code?: enum [ACCOUNT_DISCONNECTED, PROFILE_OVER_LIMIT]}` — no `code` means an ownership failure (accountId not owned by the caller/API key's profile scope) |
| `409` | Content-hash duplicate (24h window) — see §6 | `{error, details: {accountId, platform, existingPostId}}` |
| `429` | Rate limit — API limit, 25 posts/hour/account velocity limit, account cooldown, or daily platform limits | `{error, details?: object}`, plus headers `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` |

**Partial platform success**: `Post.status: "partial"` when some
platforms in one request succeed and others fail — each platform's own
outcome lives in its own `PlatformTarget.status`/`errorMessage`/
`errorCategory` entry within the same `post.platforms[]` array. Confirmed
via a full worked example (`partialPost`) in the spec.

**Upload/media-fetch failure**: no distinct top-level error code —
appears to surface as a platform-level `errorCategory` (e.g.
`platform_rejected`, `user_content`) within that platform's
`PlatformTarget` entry, same as any other platform-side rejection. Not
independently confirmed with a dedicated example (flagged in §8).

## 8. Explicitly unverified / inferred / unknown

- **Exact semantics of `timezone` versus an already-offset-bearing
  `scheduledFor`** (e.g. whether `timezone` can shift interpretation of a
  bare local time, or is purely informational/used for
  recycling-interval math) — not separately documented in the excerpts
  read. Classification: **unknown**.
- **Queue/`queuedFromProfile` configuration semantics** (how a
  per-profile posting queue's slots are defined/managed) — paths exist
  (`/v1/queue/...` referenced in a field description) but were not read
  in this pass; only the `createPost`-side fields (`queuedFromProfile`,
  `queueId`) were confirmed. Classification: **not investigated this
  pass** (would matter only if Cortex adopts Zernio's own queue feature
  instead of driving `scheduledFor` directly from Cortex's existing
  `publish_calendar`/`schedules` tables — not required for the stated
  "fixed recurring schedule" goal, since Cortex can simply compute and
  send an explicit `scheduledFor` per episode itself).
- **Media-fetch-failure-specific error shape** — inferred to surface as a
  generic platform-level `errorCategory`, not confirmed by a dedicated
  documented example. Classification: **inferred, not independently
  confirmed**.
- **Live-write behavioral correctness** — the schema above is the
  *documented* contract; it has never been exercised with a real `POST
  /v1/posts` call by this project. A live call could still reveal
  undocumented edge-case behavior. Classification: **not live-write
  verified** (expected and normal prior to any first controlled write —
  this is not a documentation gap, it is the inherent difference between
  "verified from documentation" and "verified live").
- **`components/responses/Unauthorized` and `components/responses/NotFound`
  shared-component bodies** were referenced but not separately dereferenced
  and read in this pass (their meaning — 401/404 — is unambiguous from
  context; their exact JSON body shape was not re-confirmed here).

## 9. Retracted evidence

The first-pass automated summary of the OpenAPI spec (produced by the
fetch tool's own small summarization model before the raw file was read
directly) claimed: (a) request fields `profileId`/`text`/`socialAccountIds`
instead of the verified `platforms[].platform`+`accountId` shape, and (b)
an `Idempotency-Key` header instead of the verified `x-request-id`. Both
claims are **contradicted by the raw spec** read directly in §2–§6 above
and are explicitly retracted — **not used as evidence anywhere in this
document**. This is recorded here as a caution: an AI-generated summary of
a large spec is not itself a verified source; only the raw schema text
constitutes evidence in this audit.

## 10. Implications for the current adapter (`hypogeum/publishing/zernio.py`)

No code was modified in this task. For the record, this pass surfaced
concrete discrepancies between the current adapter and the now-verified
contract, for a future corrective slice:

1. **`_parse_post_response()` reads the wrong nesting level.** It does
   `response_body["_id"]` and `response_body.get("platforms")` at the top
   level; the verified contract wraps these under `response_body["post"]`
   (`PostCreateResponse = {message, post: Post}`). **This is a confirmed
   bug**, not a gap — every successful real call would currently be
   misparsed as a malformed response (fails closed to "ambiguous," not
   unsafe, but incorrect).
2. **The 409-handling check reads the wrong nesting level.**
   `_handle_response()` checks `body.get("existingPostId")` at the top
   level; the verified shape is `body["details"]["existingPostId"]`.
   **Confirmed bug** — the current code would never actually detect this
   case and would instead fall through to "other 4xx → definitive
   rejection," misclassifying a dedup conflict as a content rejection.
3. **No handling for the 200-retry-with-`existingPost` case** (mechanism
   1 in §6) is implemented at all today. In practice this path is
   unlikely to be reached, since `submission_ledger.guard_and_prepare_attempt()`
   already prevents Cortex from re-sending the same `x-request-id`-bearing
   attempt a second time before the first resolves — but if it were ever
   reached, the current code would misparse it the same way as finding
   #1 (looking for `_id` at the wrong nesting level).
4. **No `mediaItems`/`scheduledFor`/`timezone`/`publishNow` are sent at
   all** — `_build_publish_body()` sends only `{"platforms": [{"platform",
   "accountId"}]}`. This is a **gap**, not a bug (nothing incorrect is
   asserted) — it was deliberately left unimplemented pending exactly the
   evidence this audit now provides.
5. **No distinct handling for `429`** (rate limit) — currently falls into
   the generic "other 4xx → definitive rejection" bucket. A rate limit is
   transient, not a confirmed content rejection; this is a **correctness
   gap** worth fixing (should map to ambiguous/retryable, not
   `record_definitive_failure`).
6. **No use of the richer `errorCategory`/`errorSource` enum** or the
   `403` `code` field (`ACCOUNT_DISCONNECTED`/`PROFILE_OVER_LIMIT`) — an
   enhancement opportunity (could feed `provider_repository.mark_connection_status()`
   directly on `ACCOUNT_DISCONNECTED`), not a defect.

## 11. Can Item 4 now safely be completed?

**Contract evidence gap: now resolved for the six Phase B questions**,
via official OpenAPI documentation (source tier 3 of the permitted
hierarchy) — no live POST was needed to answer them. The mandatory stop
condition in this task ("if the exact media-attachment and scheduling
contracts cannot be verified without a live POST") **does not apply** —
they were verified without one.

**Item 4 is not, however, ready to be marked complete by this audit
alone.** Completing it requires:
- Correcting the two confirmed parsing bugs (§10.1–10.2).
- Implementing `mediaItems`/`scheduledFor`/`timezone`/`publishNow`
  construction in `_build_publish_body()` against the now-verified
  schema.
- Extending fake-transport tests to cover the corrected response
  envelope, the true 409 body shape, the 200-retry-with-`existingPost`
  case, and a 429 case.
- This document does not authorize that implementation work — it is a
  read-only research artifact only, per this task's explicit scope.

## 12. Smallest next safe step

An **implementation task** (not another audit) that: (a) fixes the two
confirmed response-parsing bugs, (b) adds `mediaItems`/`scheduledFor`/
`timezone`/`publishNow` construction using this document's verified
schema, (c) adds a 429 → ambiguous/retryable classification, and (d)
extends `tests/test_publishing_zernio.py` with fake-transport cases for
all of the above — still with zero live calls, zero real operational
rows, and zero Channel Package activation. This remains squarely within
ADR-013 Decision 7 item 4's own scope (not item 5/analytics), and does
not require any live write to complete, since every field involved is now
backed by official-documentation evidence rather than inference.
