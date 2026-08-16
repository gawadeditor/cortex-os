# Protected Generation Lifecycle Reconciliation

Companion to `AUTHORIZED_GENERATION_ENVELOPE.md` and
`AUTHORIZED_GENERATION_ENVELOPE_IMPLEMENTATION_BLUEPRINT.md`. Documents
two generic defects discovered during the first real, gate-enforced
provider call (the video-domain Enforcement Cutover Canary) and the
Factory Core fixes applied — never episode/channel-specific patches.

## 1. Defect: a successful, charged execution never left `AUTHORIZED`

### Root cause

`hypogeum/authorization_lifecycle.py` already defines the full
post-authorization state machine: `AUTHORIZED -> SUBMITTING ->
SUBMITTED -> DOWNLOADED -> PENDING_EDITORIAL_REVIEW -> QUALIFIED ->
ALLOCATED -> CONSUMED` (with `TECHNICAL_VALIDATION_FAILED` and
`REJECTED` as terminal siblings). `hypogeum/submission_gate.py::
submit()`, however, never called `.transition(` on the
`AuthorizationRecord` it was handed — it durably recorded write-ahead
receipts (`hypogeum/provider_receipt.py`), settled the grant ledger
(`hypogeum/authorization_store.py`), and committed budget reservations,
but the `AuthorizationRecord` itself sat at `AUTHORIZED` forever, even
after a real, provider-acknowledged, charged execution.

This was a pure observability/correctness-of-representation defect, not
a safety one: duplicate execution was (and remains) independently
prevented by the write-ahead receipt store's idempotency-key check
(`submit()` Step 7). Nothing before this fix could actually be charged
twice; the record's *displayed* state was simply wrong.

### Fix

`hypogeum/submission_gate.py::submit()` is deliberately **not** where
this is fixed: `tests/test_authorization_authority_trust_boundary.py`
statically proves (AST-based, not substring-matched) that
`submission_gate.py` may never call `authorization_store.
persist_authorization_record()` at all — the gate settles receipts,
the grant ledger, and budget reservations for a real execution, but
never the `AuthorizationRecord`'s own persisted lifecycle state. That
boundary is deliberate (mirrors the existing rule that only
`hypogeum/authorization_authority.py` may mint/broaden authorization)
and this fix does not weaken it.

Instead, `hypogeum/authorization_store.py` (which every domain caller
already imports for its own reads) gained `record_submission_outcome(
conn, authorization_id, *, outcome, submitting_at, submitted_at=None,
provider_job_id=None)`. The **domain caller** — never the gate —
invokes it immediately after `submission_gate.submit()` returns, using
the exact outcome the gate reported:

- Every outcome (`"accepted"`, `"rejected"`, `"unknown_acceptance"`)
  advances `AUTHORIZED -> SUBMITTING` — a real provider boundary was
  genuinely entered, evidenced by `submitting_at`.
- Only `outcome="accepted"` advances further, `SUBMITTING ->
  SUBMITTED` — evidenced by `provider_job_id` and `submitted_at`, both
  fields every `AdapterResult` already carries generically (no domain
  content is inspected).
- Idempotent: a record already at or past `SUBMITTED` is returned
  unchanged; a record already at `SUBMITTING` can still advance to
  `SUBMITTED` on a later call reporting `outcome="accepted"` (e.g. a
  reconciliation pass resolving a previously-unknown acceptance).

The same function serves as a **backfill** for any record left
stranded at `AUTHORIZED` from before this mechanism existed: the
caller supplies the receipt trail's own recorded event timestamps as
`submitting_at`/`submitted_at`, never a fabricated "now", so the
durable history stays an honest record of when the real events
actually happened.

`hypogeum/muapi.py::_submit_scene_via_gate()` (the one real caller as
of this fix) now calls `record_submission_outcome()` immediately after
`submission_gate.submit()` returns, whenever `result.adapter_invoked`
is `True` (i.e. a real boundary was actually entered — a pre-boundary
denial, e.g. a bad lease, correctly leaves the record untouched at
`AUTHORIZED`), and explicitly commits the connection (unlike most of
this call chain, `persist_authorization_record()`'s own `INSERT` is
not implicitly flushed by any later DDL statement in this function, so
an explicit `conn.commit()` is required here).

Everything past `SUBMITTED` (a real file was downloaded and technically
valid, or wasn't) depends on domain-specific facts neither
`submission_gate.py` nor this new function can know generically — that
responsibility belongs to the calling domain too, via three more
generic functions in `hypogeum/authorization_store.py`:

- `advance_to_pending_editorial_review(conn, authorization_id, *,
  output_checksum, technical_validation_result, at)` —
  `SUBMITTED -> DOWNLOADED -> PENDING_EDITORIAL_REVIEW`. Idempotent
  (no-ops if already at or past `PENDING_EDITORIAL_REVIEW`); fails
  closed if the record has not reached `SUBMITTED`.
- `advance_to_technical_validation_failed(conn, authorization_id, *,
  output_checksum, failure_reason, at)` — the `DOWNLOADED ->
  TECHNICAL_VALIDATION_FAILED` sibling.
- `record_editorial_verdict(conn, authorization_id, *, reviewer,
  verdict, at)` — the **only** function in Factory Core that can reach
  `QUALIFIED`/`REJECTED`. Automated visual/technical assessment belongs
  inside `technical_validation_result`, never here — this is what makes
  it structurally impossible for an automated assessment to impersonate
  a human editorial verdict: `advance_to_pending_editorial_review()`
  cannot reach `QUALIFIED`/`REJECTED` under any input.

All four functions (`record_submission_outcome`,
`advance_to_pending_editorial_review`,
`advance_to_technical_validation_failed`, `record_editorial_verdict`)
are idempotent and fail closed on out-of-order calls.

## 2. Defect: duplicated `episodes/{id}` path segment in receipt storage

### Root cause

`hypogeum/provider_receipt.py::WriteAheadReceiptStore` expects
`assets_root` to be a bare repository asset root (e.g.
`.hypogeum-assets`) and internally appends `episodes/{execution_id}/
receipts/{idempotency_key}`. Every real production caller across every
domain, however, already holds an *episode-scoped* directory when it
constructs the store — `hypogeum/muapi.py` (3 call sites),
`hypogeum/openrouter.py`, `hypogeum/perplexity.py`,
`hypogeum/elevenlabs.py` (2 call sites), and
`hypogeum/providers/muapi_video_upscaler.py` all pass something shaped
like `_ASSETS_ROOT / "episodes" / episode_id`. The store then appended
`episodes/{execution_id}` a *second* time, producing paths like
`.hypogeum-assets/episodes/{id}/episodes/{id}/receipts/...`. This
receipt-store-shaped test doubles (which pass a bare `tmp_path`) never
exercised this defect, so it went undetected until the first real
gate-path execution.

### Fix

`resolve_episode_receipts_root(assets_root, execution_id) -> Path`
(new, `hypogeum/provider_receipt.py`) is the single canonical resolver.
It recognizes four input shapes — bare asset root, `episodes` root,
exact episode root, already-canonical receipts root — via structural
path-part comparison (never string replacement), and produces the same
canonical result for all of them. It fails closed
(`AmbiguousReceiptsRootError`) if the path already names a *different*
episode than `execution_id`, or on any directory-traversal component.
`WriteAheadReceiptStore._attempt_dir()` — the single choke point every
other method in the class calls — now delegates to it, so every
existing production caller is fixed with zero call-site changes.

`relocate_legacy_receipt_trail(legacy_root, canonical_root, *, at)`
(new, same module) is the one-shot, auditable, checksum-verified
relocation for receipt trails that were already written at the old,
incorrect location before this fix shipped. Never deletes the legacy
copy; fails closed on any checksum mismatch; returns an auditable
relocation record the caller persists (never itself).

## 3. Cost accounting: two ledgers, one documented authority

`hypogeum/schema.sql`'s `costs.run_id` is `NOT NULL` with a foreign key
to `runs.run_id`. A grant-derived, gate-enforced execution has no
`run_id` at all — it is authorized by a `RunAuthorizationGrant`
(`hypogeum.authorization_authority`), never a queue-registered run — so
it was structurally impossible to record it in `costs` directly.

**Authoritative ledger**: `run_authorization_grant_consumption`
(`hypogeum.authorization_store`). Every protected-path charge is
committed there first, exactly once, atomically with the gate
transaction. Never superseded by anything in this section.

**Read-model projection**: `protected_path_cost_projections` (new
table, `hypogeum/cost_projection.py`) — purely additive, keyed by the
ledger's own globally-unique `reservation_id`, so re-projecting the
same ledger row is always a safe no-op. Never itself a source of truth;
`reconcile_all_committed_charges()` can always losslessly rebuild it
from the ledger.

**Legacy costs table** (`hypogeum.finance`): authoritative only for
legacy, run-registered pipeline spend — unchanged by this work.

`get_reconciled_episode_total(conn, episode_id)` is the one function
that combines both tables in exact `Decimal` arithmetic.
`hypogeum.finance.write_episode_rollup()` — the existing single
computation point for the dashboard-visible per-episode total — now
calls it instead of reading `costs` alone, so every future rollup for
any episode automatically includes protected-path spend with no
per-episode or per-domain hardcoding.
