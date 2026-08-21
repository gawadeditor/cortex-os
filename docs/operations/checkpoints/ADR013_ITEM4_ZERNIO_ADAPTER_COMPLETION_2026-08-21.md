# ADR-013 Decision 7, Item 4 — Zernio Publishing Adapter — Completion Checkpoint

Status: implementation checkpoint. Fake-transport tests only. No live
Zernio call of any kind was made. No credential value was read, printed,
or exposed. No production database mutation occurred.

**Explicit status: `IMPLEMENTATION COMPLETE — NOT OPERATIONALLY ENABLED`**

---

## 1. Previous and current full HEAD

- Previous: `a6a1edba81ad7bf6eedab140c433fcb62b4aa61b`
- Current (pre-commit, this implementation): applied on top of `a6a1edb`,
  committed as this checkpoint's own commit (see final report for the
  post-commit HEAD).

## 2. Authoritative contract version and source artifact

`docs/operations/audits/ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md` —
official Zernio OpenAPI 3.1.0 spec, `info.version: "1.0.4"`
(`docs.zernio.com/api/openapi`), read directly (not an AI-summarized
excerpt). This is the sole contract source for every field name, response
envelope, and error shape implemented below.

## 3. Files changed

| File | Change |
|---|---|
| `hypogeum/publishing/provider.py` | +38/-0 — adds `MediaReference` (generic, provider-neutral media-by-URL input) |
| `hypogeum/publishing/zernio.py` | +555/- (net growth) — response-parsing bug fixes, media contract, scheduling contract, error classification |
| `tests/test_publishing_zernio.py` | full rewrite — 73 tests (was 35) |

No other file was touched. `hypogeum/publishing/provider_registry.py`,
`hypogeum/publishing/provider_repository.py`,
`hypogeum/publishing/submission_ledger.py` (items 1–3) were **not**
modified in this task.

## 4. Parsing bugs corrected

1. **Response envelope.** `_parse_post_response()` previously read `_id`/
   `platforms` at the response body's top level. The verified contract is
   `PostCreateResponse = {message, post: Post}` (201) and `{..., existingPost:
   Post}` (200 idempotent replay) — never a bare `Post`. Fixed via a new
   `_handle_success_envelope(body, envelope_key="post"|"existingPost", ...)`
   that extracts the correctly-nested object before calling
   `_parse_post_response()`.
2. **409 idempotency-conflict body.** Previously read `body["existingPostId"]`
   at the top level. The verified shape is `body["details"]["existingPostId"]`
   (content-hash dedup, a mechanism distinct from the 200-idempotent-replay
   case). Fixed in `_handle_response()`'s 409 branch. An undocumented 409
   shape (no `details.existingPostId`) is now explicitly classified
   `malformed_response` rather than silently falling through.

## 5. Media contract implemented

- `hypogeum.publishing.provider.MediaReference(url, media_type,
  thumbnail_url=None, title=None, alt_text=None)` — generic, provider-neutral,
  added to Factory Core's contract module (not Zernio-specific).
- `ZernioProvider.publish()` accepts `media: list[MediaReference]` (top-level
  `mediaItems[]`) and `platform_media: list[MediaReference]` (per-platform
  `platforms[0].customMedia[]`) — both audit-confirmed fields, serialized via
  `_serialize_media_item()` using only documented `MediaItem` fields (`type`,
  `url`, `title`, `altText`, `thumbnail`).
- **Every `MediaReference.url`/`.thumbnail_url` is validated before any
  binding/credential/ledger access**: must be `https://` with a host —
  local filesystem paths (`/Users/...`, `C:\...`), `file://`, and `http://`
  are all rejected (`ZernioInvalidMediaURLError`). `media_type` must be one
  of the documented enum. Rejected URLs never appear un-redacted in an
  exception message — `_redact_url()` strips the query string and fragment
  first, so a signed/presigned URL's signature never leaks into an error.
- **No media at all is rejected** (`ZernioMediaRequiredError`) — every
  Cortex deliverable through this adapter is video content.
- This adapter **never uploads a file, invents a URL, or calls
  `POST /v1/media/presign` / `POST /v1/media/upload-direct`** — confirmed
  by a dedicated test asserting the only URL ever passed to the transport
  is `https://zernio.com/api/v1/posts`.
- **Runtime data audit finding (unchanged by this task, recorded for the
  record):** `hypogeum/agents/publishing_agent.py`'s STEP 6/8/9/10 currently
  pass a **local** `video_path: Path` to the direct providers, never a
  public URL. `hypogeum/publishing/staging.py` already produces a public
  URL for Instagram (`StagingProvider.stage_file() -> str`). Wiring a real
  Cortex publish through this adapter will require the future orchestrator
  to stage the file first and pass the resulting URL as a `MediaReference`
  — this adapter does not do that itself, by design (explicitly out of
  scope for this task: no upload, no storage infrastructure).

## 6. Scheduling input contract implemented

- `scheduled_for: Optional[datetime]` (top-level `scheduledFor`),
  `platform_scheduled_for: Optional[datetime]` (per-platform override),
  `timezone_name: Optional[str]` (`timezone`, IANA-validated via
  `zoneinfo` when given), `publish_now: bool`, `is_draft: bool`.
- **Timezone-aware only** — a naive `datetime` is refused
  (`ZernioScheduleValidationError`); no implicit timezone policy is ever
  applied.
- **Fails closed on contradictions**: `publish_now=True` with a future
  `scheduled_for`/`platform_scheduled_for`; `is_draft=True` combined with
  `publish_now` or either scheduled-time field; a schedule not in the
  future (unless `publish_now=True`).
- Serialized to the exact `Z`-suffixed UTC ISO 8601 form used in every
  documented example (`_serialize_datetime_utc()`), regardless of the
  input datetime's original offset.
- **Does not compute Cortex's editorial calendar** — this adapter accepts
  a resolved schedule only; no recurring-slot logic was added, per this
  task's explicit exclusion.

## 7. Error/idempotency semantics implemented

| Status | Handling |
|---|---|
| `200` | Idempotent replay — `existingPost` envelope, same success path as 201, only if the post identity parses |
| `201` | Success — `post` envelope, receipt + content mapping created |
| `409` | Content-hash dedup — `details.existingPostId` → ambiguous (`content_hash_conflict`); undocumented shape → ambiguous (`malformed_response`); **never** treated as success |
| `429` | Ambiguous (`rate_limited`) — never definitive; a later attempt remains possible only after reconciliation, matching item 2's fail-closed retry policy |
| `403 ACCOUNT_DISCONNECTED` | Definitive rejection (`account_disconnected`) + best-effort `mark_connection_status(needs_reconnection=True)` on the resolved `provider_account` |
| `403 PROFILE_OVER_LIMIT` | Definitive rejection (`profile_over_limit`), no account side effect |
| `403` (no code) | Definitive rejection (`account_ownership_mismatch`) |
| `400` | Definitive rejection (`validation_error`) |
| `5xx` / transport exception | Ambiguous — unchanged from the prior slice |
| any other undocumented status | Ambiguous (`undocumented_status`) — never asserted as a confirmed rejection without evidence |

Preserved unchanged from items 1–3: Credential Manager resolution path,
durable publication intent, write-ahead attempt (`guard_and_prepare_attempt`),
idempotency-key reuse (`provider_request_key`), receipt/content-mapping
creation only inside `record_success()`, explicit-selection-with-no-fallback,
provider/platform identity separation, and zero automatic creation of real
`provider_profiles`/`provider_accounts`/`channel_provider_bindings` rows.

## 8. Fake-transport test evidence

```
pytest tests/test_publishing_zernio.py -q
73 passed
```

Covers (at minimum, per task requirement): exact 201 `{message, post}`
parsing; 200 idempotent replay (including malformed/missing-key variants);
malformed 201 (missing `post`, missing `_id`, non-JSON body); nested 409
`details.existingPostId`; undocumented/no-body 409; top-level `mediaItems`;
per-platform `customMedia`; video and image media references; local-path
rejection (POSIX and Windows forms); non-HTTPS/`file://` scheme rejection;
missing-media rejection; signed-URL query-string redaction from exception
text; top-level and per-platform schedule; timezone-aware `Z`-suffixed
serialization; naive/past/contradictory schedule rejection; `publishNow`/
`isDraft` wire presence; `429` ambiguous handling + blocked-retry-after;
both documented `403` codes (with/without account side effect); 403
without a code; transport-exception and `5xx` ambiguity; undocumented
status code ambiguity; confirmed success → receipt + content mapping;
partial multi-platform success (one platform succeeds, one is rejected,
only the successful one gets a mapping); ambiguous/failure outcomes create
neither a receipt nor a mapping; retry reuses the same durable
`x-request-id`; deliberate republish gets a new key; no fallback to direct
providers (both failure and success paths); no real socket opened; no
real credential-value leakage in exception text; no cross-database
credential/binding leakage; never calls `/v1/media/presign` or
`/v1/media/upload-direct`.

## 9. Regression evidence

```
pytest tests/test_publishing_agent.py tests/test_publishing_submission_ledger.py \
  tests/test_publishing_provider_registry.py tests/test_publishing_provider_repository.py \
  tests/test_publishing_zernio.py tests/test_publishing_youtube.py tests/test_publishing_tiktok.py \
  tests/test_publishing_meta.py tests/test_publishing_staging.py tests/test_platform_connections.py \
  tests/test_publishing_agent_db_resolution.py tests/test_pipeline.py tests/test_worker_integration.py \
  tests/test_db.py tests/test_channel_config.py tests/test_channel_pipeline_integration.py \
  tests/test_credential_manager.py tests/test_provider_boundary_inventory.py
846 passed, 2 skipped (pre-existing, unrelated)
```

## 10. Live read-only evidence

None gathered in this task. (The one public, unauthenticated `GET` to
`docs.zernio.com/api/openapi` — a documentation endpoint, not a Zernio
account/API-data endpoint — occurred in the prior contract-audit task, not
this one. This task performed zero external requests of any kind.)

## 11. Live-write evidence

**None.** No `POST`/`PUT`/`PATCH`/`DELETE` was ever sent to Zernio, in this
task or any prior one. `RequestsZernioTransport` (the production default,
real-HTTP transport) was constructed by exactly one test
(`test_constructing_default_transport_makes_no_network_call`) and its
`.post()` method was never invoked anywhere.

## 12. Production database and operational-binding state

Verified by read-only `SELECT` against `.hypogeum-assets/hypogeum.db`
before and after this task — unchanged throughout:

| Table | Row count |
|---|---|
| `provider_profiles` | 0 |
| `provider_accounts` | 0 |
| `channel_provider_bindings` | 0 |

Zero real operational rows exist or were created. The pre-existing
`secret_profiles` row (`provider='zernio', status='active', scope='factory'`,
predating all Zernio work in this project) remains unbound to any account
or binding — `ZernioProvider.publish()` still fails closed
(`ZernioBindingNotFoundError`) for every real channel today, by
construction, with no code path in this repository that could change that
without an explicit, separate, human-approved operational setup step.

## 13. Explicit remaining blockers

- No real `channel_provider_bindings`/`provider_accounts`/`provider_profiles`
  rows exist — a separate, explicit, human-approved operational setup step
  is required before any real channel can route to Zernio.
- Actual episode media URL readiness is unproven — `hypogeum/agents/publishing_agent.py`
  still passes a local `video_path` to direct providers; no orchestration
  code stages a Cortex episode's video to a public URL and constructs a
  `MediaReference` from it. This adapter's media contract is ready to
  receive one; nothing produces one yet for this specific adapter.
- Fixed recurring schedule policy (Hypogeum's own release calendar) is not
  installed anywhere — this adapter only accepts an already-resolved
  `scheduled_for`, by design.
- No live write has ever occurred against Zernio, so the documented
  (schema-verified) contract's actual runtime behavior remains unconfirmed
  in practice.
- Zernio is not registered in `DEFAULT_PROVIDER_ROUTING` and not bound to
  the Hypogeum Channel Package — selecting it for any real channel still
  requires an explicit, separate configuration change this task did not
  make.

## 14. Smallest next safe step

A **separate, explicit, human-approved operational setup task** (not
authorized or performed here) that: (a) creates real `provider_profiles`/
`provider_accounts` rows from the actual Zernio profile/account IDs
already observed in the original read-only audit, (b) creates a real
`channel_provider_bindings` row for one specific channel/platform/capability
combination, and (c) still performs zero live writes — read-only account/
profile verification only, if any live call is made at all. Only after
that step, with real bindings in place and with a real staged media URL
available from Cortex's own pipeline, would a single, separately-authorized,
isolated live-write smoke test (ADR-013 Decision 7, item 10) become
possible. This checkpoint does not authorize or begin that work.

---

## 15. Status

**IMPLEMENTATION COMPLETE — NOT OPERATIONALLY ENABLED**

All documented adapter behavior from the verified contract (response
parsing, media, scheduling, error/idempotency classification) is
implemented and covered by passing fake-transport tests. This is **not**
classified live-ready: no real operational bindings exist, no controlled
live write has occurred, actual episode media URL readiness is not yet
proven, and fixed recurring schedule policy is not yet installed.
