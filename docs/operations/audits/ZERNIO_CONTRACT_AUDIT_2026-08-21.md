# Zernio Contract and Idempotency Audit — 2026-08-21

Status: read-only audit artifact. No implementation, ADR, or schema change was made as part of this document.
Credential handling: the Zernio API key was resolved via `hypogeum.credential_manager.resolve_credential()` and used only in-process to set an `Authorization: Bearer` header for the three calls below. It is not recorded anywhere in this file, was never printed to any log, and is not present in any artifact this audit produced.

---

## 1. Official documentation evidence

Source: `https://docs.zernio.com` (root, navigation), `https://docs.zernio.com/llms-full.txt` (LLM-oriented full-text doc), `https://docs.zernio.com/api/openapi` (OpenAPI 3 spec, ~2.2MB YAML — authoritative for exact field names below).

**Servers** (`servers:` block): production `https://zernio.com/api`, local `http://localhost:3000/api`. All paths below are under `/v1`.

**Auth**: `Authorization: Bearer $ZERNIO_API_KEY`. Keys observed with `sk_` prefix (the resolved credential matched this format; 67 chars total).

**`GET /v1/accounts`** (operationId `listAccounts`):
- Query params: `profileId`, `platform`, `status` (enum `connected`/`disconnected`; omit = any status), `includeOverLimit` (bool), `page`/`limit` (must be supplied together or omitted together — omitted returns all accounts, no pagination).
- Response: `AccountsListResponse` = `{ accounts: SocialAccount[], hasAnalyticsAccess: bool, pagination?: Pagination }`.
- `SocialAccount` schema: `_id` (Zernio internal), `platform` (enum of 25 values including `youtube`/`tiktok`/`instagram`/`facebook`), `profileId` (string or embedded `Profile`), `username`, `displayName`, `profilePicture`, `profileUrl`, `isActive`, `needsReconnection`, `followersCount`*, `followersLastUpdated`*, `parentAccountId` (ads-linked accounts only). (*analytics add-on only)
- Docs description confirms disconnected/expired accounts **remain returned by default** — the `status` filter must be applied explicitly to exclude them.

**`GET /v1/profiles`** (operationId `listProfiles`):
- Query params: `includeOverLimit`, `name` (exact match), `limit`/`skip`. No params = full list.
- Response: `{ profiles: Profile[] }`. `Profile` fields observed live: `_id`, `name`, `color`, `isDefault`, `accountUsernames` (array), `userId`, timestamps.
- Docs state accounts live in exactly one profile each; one profile is a tenant/workspace container that can span every supported platform — profiles are not platform-specific.

**`GET /v1/analytics`** (operationId `getAnalytics`):
- Query params: `postId` (single-post mode, accepts Zernio or External Post IDs, auto-resolved), `platform` (default "all"), `profileId` (default "all"), `accountId`, `source` (enum `all`/`late`/`external`; "late" = posted via Zernio, "external" = synced from platform), `fromDate`/`toDate` (default: 90 days ago → today, max range 366 days), `limit` (default 50, max 100), `page`, `sortBy`, `order`.
- List response fields observed live: `overview` (`totalPosts`, `publishedPosts`, `scheduledPosts`, `lastSync`, `dataStaleness`), `posts[]`, `pagination` (`page`/`limit`/`total`/`pages` — offset-based, **no cursor field**), `accounts`, `hasAnalyticsAccess`.
- Post object fields observed live: `_id` (Zernio post ID), `latePostId`, `platform`, `platformPostUrl`, `platforms[]` (per-destination sub-results — see below), `profileId`, `publishedAt`, `scheduledFor`, `status`, `content`, `mediaType`, `thumbnailUrl`, `isAd`, `isExternal`, `analytics{}`.
- **`platforms[]` sub-object fields (per-platform result within one post)**: `accountId`, `accountUsername`, `platform`, `platformPostId` (**native platform ID, confirmed present at this nesting level**), `platformPostUrl`, `status`, `syncStatus`, `errorMessage`, nested `analytics{}`.
- `analytics{}` metric keys observed live: `clicks`, `comments`, `engagementRate`, `follows`, `igReelsAvgWatchTime`, `igReelsVideoViewTotalTime`, `impressions`, `lastUpdated`, `likes`, `reach`, `saves`, `shares`, `videoDurationSeconds`, `views`.
- Timestamps: ISO 8601 UTC (`Z` suffix) throughout.
- No incremental-sync cursor/watermark parameter is documented or observed; sync state is polled via `fromDate`/`toDate` + `overview.lastSync`/`dataStaleness`.

**Publishing (`POST /v1/posts`, not called)**: requires `platforms[].platform` + `platforms[].accountId` (Zernio account ID, not native); one request can target multiple platforms; supports caller-provided idempotency via `x-request-id` header (UUID; same UUID retried within ~5 minutes returns the original post — server-side dedup, evidenced by a documented 409 + `existingPostId` response); status lifecycle `draft → scheduled → publishing → published/partial/failed`, pollable via `GET /v1/posts/{postId}`.

---

## 2. Live read-only verification (executed)

All three calls made with `GET`, `Authorization: Bearer <redacted>`, no request body, no write parameters.

| Endpoint | UTC timestamp | Method | URL | HTTP status |
|---|---|---|---|---|
| accounts | 2026-08-21T09:28:08.089991+00:00 | GET | `https://zernio.com/api/v1/accounts` | 200 |
| profiles | 2026-08-21T09:28:09.303425+00:00 | GET | `https://zernio.com/api/v1/profiles` | 200 |
| analytics | 2026-08-21T09:28:10.305222+00:00 | GET | `https://zernio.com/api/v1/analytics` | 200 |

### accounts (sanitized)
- Top-level: `{accounts: [...], hasAnalyticsAccess: true}`. No pagination object present (none requested).
- 4 accounts returned, one per platform: `facebook`, `instagram`, `tiktok`, `youtube`.
- All 4: `isActive: true`, `needsReconnection: false` → **connected and active**, not merely present.
- All 4 share the identical `profileId._id` — one Zernio profile owns all four accounts.
- Native platform account identity is exposed via `platformUserId` and `username` fields (present on every account object). A field literally named `platformAccountId` does **not** exist on the live `SocialAccount` object — this corrects an inaccuracy in the LLM-summarized documentation excerpt gathered earlier in this audit.
- No Zernio-internal account ID is absent — `_id` present on every row.

### profiles (sanitized)
- Top-level: `{profiles: [...]}`.
- Exactly 1 profile, `isDefault: true`, `accountUsernames` is a 3-element string array (platform-account handles associated with the profile — sanitized, values not reproduced here).

### analytics (sanitized)
- Top-level: `{overview, posts, pagination, accounts, hasAnalyticsAccess: true}`.
- `overview`: `totalPosts=13`, `publishedPosts=13`, `scheduledPosts=0`, `lastSync` present, `dataStaleness.staleAccountCount=0`.
- `pagination`: `{page:1, limit:50, total:13, pages:1}` — offset-based, confirms no cursor field exists in the live response either.
- Platform values observed across the 13 posts: `{tiktok, youtube, facebook, instagram}` — all four connected platforms have analytics data.
- Native platform post IDs: **present**, nested at `posts[].platforms[].platformPostId` (not at the top level of the post object — top-level `platformPostId` is absent; this is the corrected finding, resolved by inspecting the nested structure).
- Zernio-internal post IDs: present (`posts[]._id`, plus a `latePostId` field of unclear current purpose).
- No error/warning fields were present in any of the three responses.

---

## 3. Data-model findings

See companion report (final chat response, section "Corrected domain and mapping model") for the full table. Summary: `platform` identity is correctly destination-platform-scoped in Cortex already; Zernio must never populate that column. `profileId`/`accountId`/`platformUserId`/`platformPostId` are all real, stable, already-returned identifiers with no Cortex-side representation today.

## 4. Publishing-contract findings

Zernio's one-call-many-platforms model (`platforms[]` array, per-platform `status`/`syncStatus`/`errorMessage`) does not fit Cortex's current one-adapter-call-per-platform assumption without a routing/result-shape change. See final response §4.

## 5. Idempotency findings

Zernio provides a real server-side idempotency primitive (`x-request-id`, ~5-minute dedup window, 409+`existingPostId` on conflict) that Cortex does not currently generate, persist, or send anywhere. See final response §5 for the precise gap.

## 6. Remaining unknowns

- Whether `x-request-id` dedup persists beyond ~5 minutes (docs describe only that window; behavior for a retry after a longer outage is unread).
- Full `Post` schema fields beyond what appeared in the 13 live records (small sample; e.g., no `isAd:true` or `status:failed` row was observed live).
- Exact HTTP error-response body shape for 4xx/429 (429 behavior is documented qualitatively — `X-RateLimit-Reset-After` header + exponential backoff — but no example body was captured).
- Whether `parentAccountId`/`byokCredentials`/ads-related fields have any relevance to a non-ads publishing/analytics integration (they appear ads-specific and were not investigated further as out of scope).
