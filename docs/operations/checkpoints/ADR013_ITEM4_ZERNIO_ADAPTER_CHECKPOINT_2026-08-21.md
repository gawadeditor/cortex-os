# ADR-013 Decision 7, Item 4 — Zernio Publishing Adapter — Corrective Checkpoint

Status: read-only checkpoint artifact. No implementation, ADR, or production
code was modified as part of producing this document.

**Explicit status: `PARTIAL — NOT LIVE-WRITE READY`**

---

## 1. Previous and current full HEAD

- Previous (Item 4 commit): `5cfb84e190402a65db3dd73d54fef9a123e0e998`
- Current (this checkpoint's verification base): `5cfb84e190402a65db3dd73d54fef9a123e0e998`

HEAD did not change between the Item 4 commit and this checkpoint's
verification pass — no code was added, changed, or reverted in between.
`5cfb84e` is HEAD itself (not merely an ancestor) at verification time.
Branch: `main`. `git status --short` at verification time showed only the
two permanently-protected untracked files (`-`, `docs/operations/BURN_IN_LOG.md`)
— no staged or unstaged changes.

## 2. Exact commit and files reviewed

Commit `5cfb84e` — `feat(publishing): add zernio provider adapter`:

| File | Change |
|---|---|
| `hypogeum/publishing/zernio.py` | new, 548 lines |
| `tests/test_publishing_zernio.py` | new, 656 lines, 35 tests |
| `hypogeum/publishing/provider_registry.py` | +32/-9 (registers `ZernioProvider()`) |
| `hypogeum/publishing/provider_repository.py` | +22 (two new read-only lookups) |
| `hypogeum/publishing/submission_ledger.py` | +20/-2 (`AttemptGuard.provider_request_key`) |
| `tests/test_publishing_provider_registry.py` | +22/-5 (updates the item-1-era "no Zernio" test) |

6 files changed, 1284 insertions, 16 deletions.

## 3. Architecture requirement

ADR-013 Decision 7, item 4: "Zernio publishing adapter, using fake-transport
tests only." Governed by Decisions 1–4 of the same ADR (provider ≠ platform;
replaceable provider architecture behind a generic contract; credential/
config/operational-DB ownership boundaries; durable write-ahead publishing
safety). Delivery order also requires items 1–3 precede item 4 — confirmed
already complete and unmodified by this work (generic provider contract +
routing; durable submission ledger; provider/account/profile/content
operational mappings).

## 4. Source code implemented

`hypogeum/publishing/zernio.py`:
- `ZernioProvider` implements `hypogeum.publishing.provider.PublishingProvider`
  (`provider_id="zernio"`, `supported_platforms={youtube,tiktok,instagram,facebook}`).
- `publish()` resolves an approved `channel_provider_bindings` +
  `provider_accounts` pair (read-only; never creates one), resolves the
  Zernio credential via `hypogeum.credential_manager` only, then drives the
  existing submission-ledger boundary (`guard_and_prepare_attempt` /
  `record_success` / `record_definitive_failure` / `record_ambiguous`)
  around an injectable `ZernioTransport`.
- Fails closed (raises before any transport call, before any ledger attempt
  row is created) on: unsupported platform, missing binding, disabled
  binding, binding bound to a different `provider_id`, binding/account
  platform mismatch, missing/inactive/needs-reconnection account, missing
  credential, cross-channel binding lookup, and a prior ambiguous/in-flight
  attempt (`ZernioAmbiguousRetryBlockedError`).
- `_build_publish_body()` sends only `{"platforms":[{"platform":...,
  "accountId":...}]}` plus an `x-request-id` header reusing the ledger's own
  durable per-attempt key. No content/media field. No scheduling field.
- `_parse_post_response()` extracts `_id` (provider post ID) and
  `platforms[].platformPostId` (native platform ID) — kept distinct
  throughout, never conflated, matching ADR-013 Decision 1.
- `RequestsZernioTransport` (real HTTP via `requests`) exists as the
  production default but is constructed-only, never invoked, by anything in
  this repository today — no code path calls it.

`hypogeum/publishing/provider_registry.py`: `build_default_registry()` now
registers `ZernioProvider()`. `DEFAULT_PROVIDER_ROUTING` is unchanged — no
existing caller is affected; Zernio is reachable only via an explicit
`provider_map` override, which nothing in production code passes.

`hypogeum/publishing/provider_repository.py`: `get_provider_profile()`,
`get_provider_account()` — additive, read-only lookups by internal PK.

`hypogeum/publishing/submission_ledger.py`: `AttemptGuard.provider_request_key`
— additive field surfacing an already-generated, already-persisted value;
no behavior change to any existing caller.

## 5. Database schema present

`provider_profiles`, `provider_accounts`, `channel_provider_bindings`,
`provider_content_mappings` (ADR-013 item 3, `hypogeum/schema.sql`) —
confirmed present in the production DB (`initialize_schema()` creates them
idempotently). No schema change was made by item 4; item 4 consumes this
schema through `provider_repository.py`'s existing read functions plus the
two new ones above.

## 6. Production database state

Verified by read-only `SELECT` against `.hypogeum-assets/hypogeum.db`
(no write of any kind executed):

| Table | Row count |
|---|---|
| `provider_profiles` | 0 |
| `provider_accounts` | 0 |
| `channel_provider_bindings` | 0 |

**Zero real Zernio operational rows exist.** `secret_profiles` contains
exactly one `provider='zernio', status='active', scope='factory'` row
(pre-existing since ADR-013's own audit cycle, predating this work) — its
value was not read, printed, or exposed by this checkpoint. This credential
is not bound to any operational account/profile/binding row, so
`ZernioProvider.publish()` fails closed (`ZernioBindingNotFoundError`) for
every real channel today — confirmed by code inspection, not by a live call.

`run_queue` (71 rows), `schedules` (2 rows), `worker_control` (4 rows) were
read only to confirm they exist and were not touched by item 4's commit —
their contents are unrelated to this checkpoint and were not further
inspected or modified.

## 7. Fake-adapter/fake-transport coverage

`tests/test_publishing_zernio.py` — 35 tests, re-run at this checkpoint:

```
pytest tests/test_publishing_zernio.py tests/test_publishing_provider_registry.py -q
57 passed in 1.97s
```

Covers: registry resolution when Zernio is explicitly configured; no hidden
fallback to legacy direct adapters (proven by monkeypatching `youtube.py`/
`tiktok.py`/`meta.py` functions to raise if called); credential resolution
without secret exposure; evidenced-only payload construction; every
binding/account fail-closed variant; successful response normalization with
provider-vs-native ID distinctness; content-mapping creation; retry/
idempotency via the existing submission ledger (including a deliberate-
republish case with a new `x-request-id`); ambiguous transport/409/5xx/
malformed-response handling producing no receipt or mapping; definitive-
rejection-then-permitted-retry; no real socket opened; no cross-DB
credential leakage. All use `FakeZernioTransport` and `tmp_path` SQLite
databases exclusively.

## 8. Live read-only evidence

Limited to what `docs/operations/audits/ZERNIO_CONTRACT_AUDIT_2026-08-21.md`
already recorded (2026-08-21, prior to this work): `GET /v1/accounts`,
`GET /v1/profiles`, `GET /v1/analytics` — all HTTP 200, sanitized, no
credential value reproduced. No additional live read-only call was made as
part of item 4's implementation or this checkpoint.

## 9. Live-write evidence

**None exists.** `POST /v1/posts` has never been called, in this project's
history, by anyone, at any point — confirmed both by the audit artifact
("not called") and by this checkpoint's own code/DB inspection (no
transport implementation was ever invoked with real credentials; no
production write occurred; PID 845 was not signaled, stopped, or used to
trigger anything).

## 10. Explicitly unverified or inferred behavior

- **Real video/media attachment contract: NOT verified.** No field name,
  upload mechanism, or content-reference shape for attaching actual
  video/image content to a Zernio post is documented anywhere in the
  sanitized audit or the OpenAPI-derived evidence gathered. `zernio.py`
  sends no content field at all rather than guessing one.
- **Scheduling request contract: NOT verified.** `scheduledFor` is
  confirmed only as a Post *response* field (`GET /v1/analytics`); it was
  never confirmed as a `POST /v1/posts` request field. `zernio.py` sends no
  scheduling field.
- **`POST /v1/posts` success-response shape: INFERRED, not live-write
  verified.** `_parse_post_response()` assumes the same "Post" resource
  schema observed live via `GET /v1/analytics` also describes `POST
  /v1/posts`' own response body. This is a reasonable inference (same
  named schema in the OpenAPI spec, same resource type) but is explicitly
  **not** independently confirmed by any live call to `POST /v1/posts`
  itself. Per this checkpoint's classification rule, this inference does
  not count as verified evidence.
- Full 4xx/429 error-response body shapes beyond the documented
  409+`existingPostId` case remain uncaptured (already flagged as a
  "remaining unknown" in the original audit).
- Whether Zernio's `x-request-id` dedup window behaves as documented beyond
  ~5 minutes is unread/unverified.

## 11. Safety confirmations

- Zero external/live Zernio calls made during this task (Phase A audit was
  entirely local `git`/`sqlite3` reads and a local `pytest` run).
- Zero credential values retrieved, printed, or exposed — only a `SELECT
  provider, status, scope` (no `credential_data` column) was ever queried.
- Zero production database mutation — every SQL statement executed during
  this checkpoint was a read-only `SELECT`.
- Zero queue, topic, schedule, or `worker_control` mutation — those tables
  were read (row counts only) and never written.
- PID 845 (`run_worker.py`) was not signaled, stopped, restarted, or
  replaced; confirmed alive and unchanged before and after this checkpoint.
- No worker was launched.
- No real `provider_profiles`/`provider_accounts`/`channel_provider_bindings`
  rows were created (confirmed: all three tables are empty).
- No production code was modified by this task — only this checkpoint
  document and (separately) its own commit are added.

## 12. Current blockers

- Real video/media attachment contract is not verified.
- Scheduling request contract is not verified.
- `POST /v1/posts` success-response shape is inferred, not live-write
  verified.
- Real provider profile/account/channel-binding rows are not installed —
  `ZernioProvider.publish()` cannot succeed for any real channel today.
- No controlled Zernio write has occurred, ever.

## 13. Smallest next safe step

A read-only, sanctioned audit pass (no write, no credential exposure
beyond in-process header use, same discipline as the original
`ZERNIO_CONTRACT_AUDIT_2026-08-21.md`) that specifically targets the three
open contract questions:
1. The exact `POST /v1/posts` request field(s) for attaching video/image
   content (re-reading the OpenAPI spec's `requestBody` schema for that
   operation specifically, which the original audit did not capture in
   full).
2. Whether/how a scheduling field appears in that same request schema.
3. The `POST /v1/posts` response envelope, ideally via one single,
   explicitly-authorized live write in a fully isolated, disposable test
   context (item 10 in ADR-013's own delivery order) — or, short of that,
   at minimum confirming the response schema section of the OpenAPI spec
   for this specific operation without any live call.

This audit step is itself still read-only research, not implementation —
the adapter should not be modified until its findings are recorded as a new
sanitized evidence artifact, the same way the original audit was.

---

## 14. Status

**PARTIAL — NOT LIVE-WRITE READY**

Item 4 is classified as **partial scaffold**: the routing, credential-
resolution, binding/account fail-closed validation, durable submission-
ledger integration, and idempotency-key handling are real, tested,
production-shaped code. The three items required for a "complete"
classification — real media attachment, scheduling request fields, and
write-response parsing, all supported by verified (non-inferred) evidence —
are not met. No inference is presented here as implementation completeness.
