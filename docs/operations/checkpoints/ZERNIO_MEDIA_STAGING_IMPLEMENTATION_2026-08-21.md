# Durable Zernio Media Staging — Implementation Checkpoint

Status: implementation checkpoint. Fake transports only. No live upload,
presign call, or any external request was made. No credential value was
read, printed, or exposed. No production database mutation occurred.

**Explicit status: `IMPLEMENTATION COMPLETE — NOT OPERATIONALLY ENABLED — NO MEDIA UPLOADED`**

---

## 1. Previous and current full HEAD

- Previous: `947e9af11a8c7a6d4a9e39d42461fd101ab310d1`
- Current: applied on top of `947e9af`, committed as this checkpoint's own
  commit (see final report for the post-commit HEAD).

## 2. Architecture / source / schema / runtime changes

**Architecture**: implements the blocker identified in
`docs/operations/audits/ZERNIO_OPERATIONAL_ENABLEMENT_READINESS_2026-08-21.md`
§4/§7 -- "no working public-media-URL path exists for any platform" --
via a **generic** Factory-Core media-staging contract with Zernio as one
provider implementation, per this task's explicit architectural
direction: `META_STAGING_BASE_URL`/the existing Meta-specific
`hypogeum.publishing.staging` module was **not** revived, configured, or
modified.

**Source added**:
- `hypogeum/publishing/media_staging.py` (new) -- Factory Core, provider-
  neutral. `SourceMedia`/`StagedMediaResult` dataclasses, `compute_sha256()`,
  `MediaStagingProvider` Protocol, `staged_result_to_media_reference()`
  (converts into the existing generic `hypogeum.publishing.provider.MediaReference`),
  and the durable write-ahead ledger
  (`guard_and_prepare_staging_attempt`, `mark_staging_upload_ready`,
  `mark_staging_uploading`, `record_staging_uploaded`,
  `record_staging_definitive_failure`, `record_staging_ambiguous`) --
  mirrors `hypogeum.publishing.submission_ledger`'s pattern exactly (same
  `BEGIN IMMEDIATE` concurrency-safety mechanism, same "commit before any
  external call" philosophy, same ambiguous-blocks-retry semantics).
  Contains no Zernio-specific (or any other provider-specific) request/
  response field name anywhere.
- `hypogeum/publishing/zernio_media_staging.py` (new) -- Zernio-specific,
  outside Factory Core. `ZernioMediaStagingProvider` implements
  `POST /v1/media/presign` + `PUT <uploadUrl>` through two injectable
  transport methods (`presign`, `upload`). Never calls `POST /v1/posts` or
  `POST /v1/media/upload-direct`.

**Schema added** (`hypogeum/schema.sql`, purely additive): `media_staging_attempts`
table + two indexes (`idx_media_staging_attempts_scope`,
`idx_media_staging_attempts_state`). No existing table was modified. Not
applied to the production database in this task (verified: 0 rows in the
production DB's `media_staging_attempts` after this work).

**Runtime wiring**: no production orchestration code
(`hypogeum/agents/publishing_agent.py` or any Channel Package
configuration) was modified. The composed
`SourceMedia -> ZernioMediaStagingProvider.stage() -> StagedMediaResult ->
staged_result_to_media_reference() -> MediaReference -> ZernioProvider.publish()`
flow is proven end-to-end by
`tests/test_publishing_zernio_media_staging.py::TestRuntimeIntegration::test_full_flow_stage_then_publish`
(fake transports on both the staging and publishing sides) -- ready for a
future orchestrator to call, but not itself wired into any real call
path. This is a deliberate scope decision: wiring it into
`publishing_agent.py` would require either activating Zernio for
Hypogeum or bypassing/weakening `hypogeum.agents.production_output_gate`'s
existing authorized-output boundary, both explicitly prohibited for this
task.

## 3. Exact official contract used

`docs/operations/audits/ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md` §4
-- official Zernio OpenAPI 3.1.0 spec, version 1.0.4
(`docs.zernio.com/api/openapi`), re-read directly from the same locally-
saved raw YAML gathered in that prior task (no fresh external fetch was
needed or performed in this task):

- `POST /v1/media/presign`: request `{filename (required), contentType
  (required, fixed 20-value MIME enum), size? (bytes, max 5GB)}`;
  response `200: {uploadUrl (https, PUT target, expires 1h), publicUrl
  (https, durable reference), key, expiresIn (always 3600)}`; `400`
  documented validation-error body (missing filename / unsupported
  contentType / size out of range) -- the only response this
  implementation treats as a confirmed, definitive rejection; `401` via a
  generic, never-independently-verified-for-this-operation `$ref`.
- `PUT <uploadUrl>`: raw bytes, no documented request/response schema for
  this step (opaque cloud storage, not a Zernio-authored endpoint) --
  every non-2xx outcome is therefore ambiguous by design, never
  definitive, for lack of evidence to classify any specific status
  confidently.

## 4. Durable staging states

`prepared -> presigning -> upload_ready -> uploading -> uploaded ->
[expired]`, with `failed_definitive` and `failed_ambiguous ->
reconciliation_required -> {uploaded|failed_definitive|failed_ambiguous}`
as the failure branches. Only `failed_definitive` and `expired` permit a
brand-new attempt; a `failed_ambiguous`/`reconciliation_required` latest
attempt blocks any new external call. A confirmed `uploaded` result with
no `expires_at` (Zernio's own `publicUrl`, per the contract, carries no
documented expiry) is reused indefinitely; one with a past `expires_at`
is lazily transitioned to `expired` on the next guard call and never
reused. Two durable pre-call commits guard the two external boundaries
(`presigning` before the presign call, `uploading` before the upload
call), exactly mirroring `submission_ledger.guard_and_prepare_attempt()`'s
single-boundary pattern applied twice. Concurrency safety uses the
identical `BEGIN IMMEDIATE` + advance-past-`prepared`-before-returning
mechanism -- verified via an 8-thread test: exactly 1 `proceed`, 7
`blocked`.

## 5. Security / redaction behavior

- Credential resolution reuses `hypogeum.publishing.zernio._resolve_zernio_credential`
  (single-sourced, not duplicated) -- Credential Manager only, never an
  environment variable, never logged/returned/persisted.
- The presigned `uploadUrl` (query-string-bearing, time-boxed) is **never
  persisted anywhere** -- not even redacted; only the resulting durable
  `publicUrl` is stored, and only after this module's own contract
  (audit-verified) confirms that field is the intended, stable reference.
- `metadata_json` and `last_error_message` are sanitized before
  persistence -- credential-shaped key names (`api_key`, `access_token`,
  `authorization`, `upload_url`, `signed_url`, etc.) are redacted, and the
  same `Bearer`/`Authorization:` regex redaction from
  `submission_ledger.py`/`provider_repository.py` is reused.
- A dedicated test confirms the fake presign response's signed query
  string (`?X-Amz-Signature=SUPERSECRET`) never appears anywhere in the
  `media_staging_attempts` table after a full successful stage.

**Bug found and fixed during test-writing** (before any commit): the
initial implementation validated `publicUrl`'s HTTPS scheme *after* the
upload transport call had already run, using the wrong exception type
(`ZernioMediaValidationError` instead of the ambiguous-outcome path), and
left the durable row abandoned mid-`uploading` with no terminal state
recorded on a non-https `publicUrl`. Fixed by moving that validation into
presign-response handling (alongside the existing `uploadUrl` HTTPS
check), before any upload byte is ever sent, and folding it into the same
`record_staging_ambiguous()` + `ZernioMediaAmbiguousOutcome` path as
every other malformed-presign-response case. Caught by
`test_non_https_public_url_is_ambiguous` before this was ever committed.

## 6. Tests and results

```
pytest tests/test_publishing_media_staging.py tests/test_publishing_zernio_media_staging.py -q
50 passed
```

Covers (at minimum, per task requirement): local file validation (missing,
empty, stale sha256, stale size, oversize, unsupported MIME -- all before
any credential/transport access); successful presign + upload (request
body correctness, Content-Type header, byte count); malformed presign
response (missing keys, non-JSON, non-https `uploadUrl`/`publicUrl`);
documented presign `400` (definitive, permits later retry) vs.
undocumented `4xx`/`5xx` (ambiguous); transport exception before presign
acknowledgement and during upload (both ambiguous); upload `4xx`/`5xx`
("uncertain completion", both ambiguous, never definitive); credential
secrecy; signed-URL query-string redaction; durable state transitions;
8-thread concurrent-claim safety; reuse of a valid non-expired staged
result (no new transport calls); refusal to reuse an expired result;
retry/idempotency (ambiguous blocks retry, definitive-failure permits a
new attempt); no duplicate uploads for the same scope+content; different-
deliverable independence; `None`-channel_id scope; conversion to
`MediaReference`; the full Zernio runtime path (stage local file, publish
via `ZernioProvider`, verify the staged `public_url` is exactly what
reached the publish request body); confirmation that the legacy Meta
`staging.py` path is never touched by Zernio staging; no real socket; no
production DB mutation.

Full regression:
```
pytest tests/test_publishing_media_staging.py tests/test_publishing_zernio_media_staging.py \
  tests/test_publishing_zernio.py tests/test_publishing_agent.py \
  tests/test_publishing_submission_ledger.py tests/test_publishing_provider_registry.py \
  tests/test_publishing_provider_repository.py tests/test_publishing_youtube.py \
  tests/test_publishing_tiktok.py tests/test_publishing_meta.py tests/test_publishing_staging.py \
  tests/test_platform_connections.py tests/test_publishing_agent_db_resolution.py \
  tests/test_pipeline.py tests/test_worker_integration.py tests/test_db.py \
  tests/test_channel_config.py tests/test_channel_pipeline_integration.py \
  tests/test_credential_manager.py tests/test_provider_boundary_inventory.py
896 passed, 2 skipped (pre-existing, unrelated)
```
`tests/test_publishing_staging.py` (the existing Meta-specific staging
tests) pass unchanged -- confirms this task did not touch that module.

## 7. Production database state

Verified by read-only `SELECT` against `.hypogeum-assets/hypogeum.db`,
before and after this task:

| Table | Count |
|---|---|
| `provider_profiles` | 0 |
| `provider_accounts` | 0 |
| `channel_provider_bindings` | 0 |
| `media_staging_attempts` | 0 |

`.env`/`META_STAGING_BASE_URL` unmodified (confirmed no diff). Zero real
media was uploaded, staged, or referenced for any real episode
(`ep_f3619022` was not touched).

## 8. Fake / live-read-only / live-write classification

Fake transports only, both sides (presign and upload). Zero live read-only
calls in this task (the official spec evidence was already saved locally
from a prior task; no fresh fetch was needed). Zero live writes -- no
`POST /v1/media/presign`, no `PUT` to any URL, no `POST /v1/posts`.

## 9. Remaining blockers (dependency order, from the readiness audit)

1. **Media delivery -- now implementable, not yet exercised live.** This
   task closes the "no staging mechanism exists" gap structurally (a
   working, tested, fake-transport-verified Zernio presign+upload path
   now exists) but does not itself constitute a live-verified media path
   -- the first real presign+upload call has never been made.
2. **Episode metadata correctness** (`ep_f3619022`'s `metadata_source`
   title/description are empty; two non-identical candidate titles exist)
   -- unchanged, not addressed by this task.
3. **Operational mapping installation** (`provider_profiles`/
   `provider_accounts`/`channel_provider_bindings`) -- still zero real
   rows; unchanged, not addressed by this task.
4. **Fixed-schedule decisions** (Shorts offset/cadence, per-platform
   timing, minimum lead time, missed/duplicate-slot policy) -- unchanged,
   not addressed by this task.
5. **A single, separately-authorized, isolated live-write smoke test**
   (presign + upload first, then eventually `POST /v1/posts`) -- only
   after 1-4.

## 10. Smallest next safe step

A separate, explicit, human-approved task performing exactly one
controlled, isolated live read-only or minimal-write verification of the
presign+upload path -- e.g. a single small test file, not a real episode
asset -- to confirm the documented contract (§3 above) holds in practice
before ever routing a real deliverable through it. This checkpoint does
not authorize or begin that work.

---

## 11. Status

**IMPLEMENTATION COMPLETE — NOT OPERATIONALLY ENABLED — NO MEDIA UPLOADED**

The generic media-staging contract and the Zernio presigned-upload
provider are fully implemented against the verified contract, durably
safe (write-ahead, concurrency-tested, no-duplicate-upload, reuse/expiry-
aware), security-reviewed (credential and signed-URL redaction verified
by tests), and composable end-to-end with the existing `ZernioProvider`
into a `MediaReference` -- entirely via fake transports. No real media has
ever been uploaded through this path, no real operational rows exist, and
no production code or configuration was changed.
