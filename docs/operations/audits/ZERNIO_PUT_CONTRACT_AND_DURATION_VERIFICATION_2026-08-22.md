# Zernio PUT Contract Verification + Platform Duration Constraints — 2026-08-22

Status: verified-contract documentation for the safe pipeline-wiring phase
(aggregation model: one deliverable → one Zernio provider post →
`platforms[]`). No live PUT, POST, or any other mutating Zernio call was
made while producing this document — every claim below is either (a) from
a fresh, read-only fetch of official documentation performed today, or (b)
explicitly marked as undocumented/unverified with fail-closed handling
retained in code. No Short was trimmed, rerendered, or otherwise edited to
meet any duration figure below — these are recorded findings only.

---

## 1. `PUT /v1/posts/{postId}` — verified via `docs.zernio.com/posts/update-post`

Fetched live, read-only, today (2026-08-22). Two independent targeted
fetches of the same page were made (once for the general contract, once
specifically re-confirming the response envelope key) and agreed.

| Question | Answer | Status |
|---|---|---|
| Method + path | `PUT /v1/posts/{postId}` | **Verified — documented** |
| Success status code | `200` | **Verified — documented** |
| Success response envelope | `{"message": ..., "post": {...}}` — wrapped under the **`"post"`** key | **Verified — documented** |
| Envelope key vs. POST's idempotent-replay key | `"post"` is the SAME key `POST /v1/posts`'s own `201` uses. **Never** `"existingPost"` — that key is exclusive to `POST`'s own `200` idempotent-replay case and does not apply to `PUT` at all | **Verified — documented, and explicitly distinguished from the adjacent, easily-confused key** |
| `platforms[]` replace-vs-merge semantics | **NOT DOCUMENTED.** The page states only "Each item must include platform and accountId" — it never states whether sending `platforms` in a PUT body replaces the post's existing platform list or merges/adds to it | **Unverified — explicitly undocumented** |
| Partial per-platform success/failure representation | `HTTP 207` is **listed** as a possible status ("partial success scenarios") but its response body is documented as **`Empty`** — no field names, no example, no shape of any kind | **Unverified — status code exists, body shape does not** |
| TikTok-specific settings placement | Root-level `tiktokSettings` field, "merged into each platform's `platformSpecificData`, with platform-specific settings taking precedence" — same placement documented for `POST` | **Verified — documented, identical to POST** |

### 1.1 Implementation consequence — already correctly handled, no change made this turn

`ZernioProvider._handle_put_response()` (`hypogeum/publishing/zernio.py`)
parses a `200` using the `"post"` key and delegates every other status
(400/403/409/429/5xx, and the undocumented-body `207`) to the same
ambiguous/fail-closed handling `_handle_response()` already uses for any
status it doesn't recognize. **`207` is never asserted as a partial
success** — there is no evidence for what its body contains, so it falls
through to ambiguous handling rather than being guessed at.

Because `platforms[]` replace-vs-merge is undocumented,
`publish_multi_platform()`'s retry path does **not** send a bare list of
only the newly-added platforms. It first issues a read-only `GET` of the
existing post, and PUTs the **union** of the existing post's current
`platforms[]` plus the newly-submitted platforms' entries. This is correct
regardless of which (undocumented) semantics Zernio actually implements:
if PUT merges, the existing entries are redundant but harmless; if PUT
replaces, omitting them would have silently dropped already-published
platforms from the post. This GET-then-union-then-PUT design predates
this turn's contract re-verification and is confirmed, not newly built,
by the findings above.

### 1.2 What remains genuinely unknown

- The exact JSON shape of a `207` response is not published anywhere
  found. If Zernio ever returns one in production, this adapter will
  record it as `ambiguous` and require manual reconciliation — by design,
  not by omission.
- Whether `platforms[]` on `PUT` is authoritative-replace or merge is not
  published. The GET-then-union strategy sidesteps needing to know, but a
  live, authorized PUT (out of scope for this phase) would be the only
  way to observe the real behavior directly.

---

## 2. Deliverable-duration constraints — verified per platform, no editorial change made

Recorded for the routing/readiness layer only. **No Short was trimmed or
rerendered.** Selecting a platform in the Zernio UI does not by itself
prove a given media file will be accepted — these are the platforms' own
(or their documented intermediary's own) published constraints, checked
against real `ffprobe`-measured duration by
`hypogeum.publishing.cross_platform_readiness`, never assumed.

| Platform | Constraint | Source | Fetched |
|---|---|---|---|
| **YouTube Shorts** | Maximum 3 minutes (180s) | `support.google.com/youtube/answer/10059070`: "short-form videos that are up to 3 minutes long" | 2026-08-22 |
| **TikTok** | **No fixed global duration.** Maximum is dynamic, per-creator, returned by the `creator_info` API's `max_video_post_duration_sec` field — API clients "must check" that value before posting | `developers.tiktok.com/doc/content-sharing-guidelines`: "API clients must check if the duration of the to-be-posted video follows the `max_video_post_duration_sec` returned in the creator_info API" | 2026-08-22 |
| **Instagram Reels** | API technically accepts **3s–15min** (`"Duration: 15 mins maximum, 3 seconds minimum"`). A separate, undocumented-by-exact-numbers eligibility check governs whether a given upload actually appears on the **Reels tab** vs. publishing as a regular video post — the API's own words: "the reel may not meet eligibility requirements or may not be selected by our algorithm" | `developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/` | 2026-08-22 |
| **Facebook Reels (via Zernio)** | **3–60 seconds** | `docs.zernio.com/platforms/facebook`: "Duration: 3–60 seconds" (under Reel requirements) | 2026-08-22 |

### 2.1 Genuine discrepancy — Facebook Reels: Zernio (60s) vs. Meta's own raw API guidance

Meta's own platform documentation
(`developers.facebook.com/docs/video-api/guides/reels-publishing`, per the
prior forensic pass in this task chain) states a **90-second** Reels
maximum at the raw Graph API level, while Zernio's own platform-contract
page — re-fetched today and unchanged — states a stricter **60-second**
maximum. Since every autonomous submission in this system goes through
Zernio as the intermediary, never Meta's raw API directly, **Zernio's
stricter 60s figure is the one that governs** what this system may
autonomously submit, regardless of what Meta's own API would separately
tolerate. This is recorded as a real, confirmed constraint mismatch
between two authoritative sources — not a bug, not something to
reconcile in this phase.

### 2.2 What this phase did NOT do

- No Short's real measured duration was compared against this table as an
  editorial decision. `cross_platform_readiness.py` already performs this
  comparison at runtime, per (deliverable, platform), using the real
  `ffprobe`-measured duration — this document only records the
  constraint values themselves and their sources.
- No 60-second (or any other) target duration was proposed, adopted, or
  implemented for future Shorts production. That remains a decision for
  a later, explicitly authorized phase.

---

## 3. Scope note

Everything in this document was produced read-only: two documentation
fetches for the PUT contract, four documentation fetches for the duration
constraints (one per platform). No credential was read or exposed. No
Zernio POST/PUT/GET/PATCH/DELETE was issued. No media was uploaded. No
production database was touched in the production of this document.
