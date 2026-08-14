# Implementation Blueprint — Authorized Generation Envelope

Status: **BLUEPRINT ONLY — NO RUNTIME CODE IMPLEMENTED**

Companion to `AUTHORIZED_GENERATION_ENVELOPE.md` (the architecture this
blueprint implements). Read that document first — this one assumes its
vocabulary (envelope, gate, receipt, budget scope) without redefining it.

This blueprint specifies exact files, interfaces, and a bounded commit
sequence. It contains no runtime code. Each commit below is independently
revertable and independently testable — none may be combined with
another in the same commit.

---

## File plan

### New files (Factory Core — generic, no Hypogeum-specific content)

| Path | Purpose |
|---|---|
| `hypogeum/execution_envelope.py` | Envelope dataclass/schema (§3 of the architecture doc), `compute_envelope_id()`, `EnvelopeSchemaError` |
| `hypogeum/authorization_lifecycle.py` | The state machine (§4): `AuthorizationState` enum, `ALLOWED_TRANSITIONS`, `transition()` with ownership checks, `invalidate_on_source_change()` |
| `hypogeum/submission_gate.py` | `verify_and_consume_envelope(envelope, *, live_sources, receipt_store, lease_store) -> GateResult` — the 20 checks (§5), in order, fail-closed on the first failure |
| `hypogeum/provider_receipt.py` | Receipt dataclass (§6.1), `WriteAheadReceiptStore` protocol/interface (persistence-agnostic — file-backed for now, matches this repo's existing JSON-file convention; swappable later per `CORTEX_ARCHITECTURE.md`'s "Infrastructure should be replaceable" rule) |
| `hypogeum/budget_scopes.py` | `BudgetScope` enum (§7.1), `CircuitBreaker` (§7.2 stop conditions), reservation/release primitives |
| `hypogeum/providers/fake_provider_adapter.py` | The one sanctioned test bypass (§8.2.5) — raises on any attempt to import `requests`/`httpx`/perform socket I/O, so it is *structurally* incapable of network access, not merely conventionally unused in tests |
| `tools/migrate_legacy_manifest_to_envelopes.py` | The one-time, auditable conversion tool (§8.2.6) |
| `docs/architecture/AUTHORIZED_GENERATION_ENVELOPE.md` | Already created this turn |
| `docs/architecture/AUTHORIZED_GENERATION_ENVELOPE_IMPLEMENTATION_BLUEPRINT.md` | This file |

### Modified files (per migration wave, §8.2 of the architecture doc)

| Path | Change |
|---|---|
| `hypogeum/muapi.py::submit_scene()` | Add a required `envelope` parameter; call `verify_and_consume_envelope()` as the first statement; remove the ability to call with only a raw `scene_spec` once all callers are migrated (Commit 4/9 boundary, never before) |
| `hypogeum/agents/video_generation_agent.py::generate_scene_video()` | Construct an envelope from the scene/plan/Channel Package context before calling `submit_scene()` |
| `hypogeum/agents/clip_repair_agent.py` (3 call sites: `fill_coverage_deficits`, `fill_confirmed_missing_shots`, and the `_submit_canary`-equivalent internal path already extracted) | Same |
| `tools/muapi_canary_submit.py` | Construct an envelope from its already-verified 13-gate preflight state instead of a bare `scene_spec`; its own preflight becomes envelope-construction, not a separate parallel check |
| `hypogeum/muapi.py::generate_reference_image()`, `generate_thumbnail()` (image domain, later wave) | Same pattern |
| `hypogeum/elevenlabs.py::generate_narration_segment()`, `generate_music_track()` (voice/music domain, later wave) | Same pattern |
| `hypogeum/agent.py::run_agent()` (text/LLM domain, later wave) | Same pattern |
| `hypogeum/perplexity.py::query_perplexity()` (research domain, later wave) | Same pattern |
| `hypogeum/providers/muapi_video_upscaler.py` (upscale domain, later wave) | Same pattern |

### Persistence location

- Receipt store: `.hypogeum-assets/episodes/{episode_id}/receipts/` (file-backed initially, one JSON per receipt, write-ahead as specified in §6.2 — matches this repo's existing `.hypogeum-assets/episodes/{episode_id}/canary_receipts/` convention, generalized). Never `hypogeum.db` directly for the write-ahead row (durability-critical, must survive independently of SQLite lock contention); a later wave may add a DB-backed index over the same files.
- Envelope/authorization store: `.hypogeum-assets/episodes/{episode_id}/authorizations/{authorization_id}/v{version}.json` — one immutable file per version, matching §4.4's "never edited in place, new version on change" rule at the filesystem level, not just in application logic.
- Lease store (gate check 19): a new `runtime_leases` table in `hypogeum/db.py`'s schema (via `_V2_COLUMN_MIGRATIONS`/new table in `hypogeum/schema.sql`) — the one piece of this design that genuinely needs a real database for atomic acquire-with-expiry semantics a flat file cannot safely provide.

### Migration tool

`tools/migrate_legacy_manifest_to_envelopes.py` — reads an existing
`generation_authorization_manifest_*.json` or `canary_authorization_*.json`,
for each entry constructs an envelope with
`audit_metadata.creation_reason = "migrated_from_legacy_manifest"` and
`audit_metadata.source_file_checksum`, writes it to the authorization
store, and leaves the legacy manifest file untouched (read-only source,
never deleted) as permanent historical evidence.

### Shared gate integration point

`hypogeum/submission_gate.py::verify_and_consume_envelope()` is the
single function every migrated caller imports. No caller constructs its
own inline copy of the 20 checks — this mirrors the existing, tested
precedent this session already established
(`hypogeum/canary_preflight.py` being shared by `tools/muapi_canary_submit.py`
and `tools/muapi_batch_preflight.py`).

### Caller migration order

Matches architecture doc §8.2, expanded to concrete call sites:

1. `tools/muapi_canary_submit.py` (smallest blast radius, already
   envelope-shaped in spirit, best regression-test coverage to build
   from)
2. `hypogeum/agents/clip_repair_agent.py::fill_confirmed_missing_shots()`
   (already closest to envelope semantics per §1.1)
3. `hypogeum/agents/clip_repair_agent.py::fill_coverage_deficits()`
4. `hypogeum/agents/video_generation_agent.py` (first-pass — largest
   blast radius, must be last within the video domain, and only after
   1-3 are live and observed)
5. `hypogeum/muapi.py::submit_scene()` flips to *requiring* an envelope
   (no more bare `scene_spec` path) — this is the commit that actually
   closes the video-domain gap, and only happens after 1-4 are each
   independently verified in production for at least one full cycle.

### Provider adapter changes

`submit_scene()`'s internal `_submit_http()`/`_poll_until_complete()`/
`_download_file()` sequence becomes the "provider adapter" referenced in
architecture §2 — it moves to accepting a validated envelope's fields
directly (never re-deriving them), and its own internal fallback
(`MUAPI_ALLOW_PAID_FALLBACKS` tier-2-to-tier-3) becomes a
separately-authorized envelope (gate check 18), not an in-function
`if allow_fallbacks:` branch as it is today.

### Idempotency mechanism

Deterministic `idempotency_key = sha256(f"{authorization_id}:{authorization_version}:{provider_operation}")`
— computed once at envelope-authorization time, stored on the envelope,
never regenerated at submission time (regenerating it would defeat its
purpose across a crash-and-resume).

### Tests required (maps to architecture doc §1.7/§8.2 item 8 and the
20-item regression list from the parent task)

| Test file | Proves |
|---|---|
| `tests/test_execution_envelope.py` | Schema validation, content-addressing (`envelope_id` recomputation), immutable-field enforcement |
| `tests/test_authorization_lifecycle.py` | Every transition table in architecture §4.1/4.2; illegal transitions raise; source-change invalidation creates a new version and preserves the old one immutably |
| `tests/test_submission_gate.py` | All 20 checks individually (each one, changed by exactly one field, blocks); zero network access on any failure path; a fake provider adapter is the only way to reach "submitted" in tests |
| `tests/test_provider_receipt_recovery.py` | Every crash scenario in architecture §6.3, using a fake provider adapter with injected failure points |
| `tests/test_budget_circuit_breaker.py` | Every stop condition in architecture §7.2; retry/fallback require separate budget reservation |
| `tests/test_call_site_coverage.py` | Repository-wide: every function in architecture §1.1-§1.4's tables either (a) calls `verify_and_consume_envelope()` before any network-capable call, or (b) is explicitly listed in an allowlist file this test also verifies is empty for production code paths. This is the test that structurally proves architecture §1.7's latent bypasses are closed, not merely documented. |
| `tests/test_migrate_legacy_manifest.py` | Migration tool produces envelopes with correct `audit_metadata`, never claims original-invariant generation, never deletes the source file |

### Rollout gates

Each commit in the sequence below ships behind a config flag
(`EXECUTION_ENVELOPE_ENFORCEMENT_MODE`, values `"off"` /
`"shadow"` / `"enforced"`, mirroring this repo's existing
`SMOKE_TEST_MODE`/`MUAPI_DRY_RUN` convention) so that:

- `"shadow"` mode runs the gate and records what it *would* have done
  without blocking, for one full observation cycle per migration wave —
  gives real production evidence before enforcement, without repeating
  the burn-in-discipline violation of shipping speculative blocking
  behavior untested against live traffic shape.
- `"enforced"` is the only mode in which a gate failure actually blocks
  a provider call.
- The flag defaults to `"off"` until Commit 3 (gate exists but nothing
  calls it yet), then `"shadow"` through the caller-migration commits,
  then `"enforced"` only after §8.2 step 8's call-site-coverage proof
  passes for that domain.

### Rollback strategy

Every commit is revertable independently because each caller migration
(Commits 5-7 below) is additive — the caller gains an envelope-
constructing wrapper but `submit_scene()` itself does not yet reject the
old calling convention until Commit 9. A revert of any single caller
migration commit leaves the system in its immediately-prior, already-
tested state. Commit 9 (enforcement flip) is the only commit with a
required rollback companion: reverting it must be a one-line flag change
(`EXECUTION_ENVELOPE_ENFORCEMENT_MODE` back to `"shadow"`), never a code
revert, so an emergency rollback does not require a deploy.

---

## Bounded commit sequence

Each numbered item below is exactly one commit. No two are combined.

1. **Envelope schema and state machine.**
   Files: `hypogeum/execution_envelope.py`, `hypogeum/authorization_lifecycle.py`, `tests/test_execution_envelope.py`, `tests/test_authorization_lifecycle.py`.
   No caller changes. Pure additions.

2. **Write-ahead authorization and receipt store.**
   Files: `hypogeum/provider_receipt.py`, `hypogeum/db.py` (new `runtime_leases` table via schema migration), `tests/test_provider_receipt_recovery.py`.

3. **Mandatory shared submission gate.**
   Files: `hypogeum/submission_gate.py`, `hypogeum/budget_scopes.py`, `hypogeum/providers/fake_provider_adapter.py`, `tests/test_submission_gate.py`, `tests/test_budget_circuit_breaker.py`.
   Gate exists, is fully tested against the fake adapter, but nothing in production calls it yet (`EXECUTION_ENVELOPE_ENFORCEMENT_MODE` has no live effect).

4. **MUAPI adapter conversion.**
   File: `hypogeum/muapi.py` — `submit_scene()` gains the `envelope` parameter and calls the gate in `"shadow"` mode; internal fallback logic becomes envelope-field-driven (gate check 18) rather than an inline flag branch. Old bare-`scene_spec` calling convention still works unchanged.

5. **Canary migration.**
   File: `tools/muapi_canary_submit.py` constructs a real envelope from its existing 13-gate preflight state.

6. **Coverage-repair migration.**
   File: `hypogeum/agents/clip_repair_agent.py` (both `fill_confirmed_missing_shots()` and `fill_coverage_deficits()`).

7. **First-pass video generation migration.**
   File: `hypogeum/agents/video_generation_agent.py`.

8. **Legacy-manifest migration tool.**
   File: `tools/migrate_legacy_manifest_to_envelopes.py`, `tests/test_migrate_legacy_manifest.py`. Run once against `ep_f3619022`'s existing manifests as the first real-world proof, producing envelopes tagged `migrated_from_legacy_manifest` — does not touch the legacy files themselves.

9. **Enforcement flip + legacy-path removal (video domain).**
   File: `hypogeum/muapi.py` — `submit_scene()` now requires a valid, gate-approved envelope; bare `scene_spec` calls raise. `EXECUTION_ENVELOPE_ENFORCEMENT_MODE` for the video domain defaults to `"enforced"`. Only after Commits 4-8 have each been independently observed in production for one full cycle.

10. **Repository-wide bypass proof and burn-in.**
    File: `tests/test_call_site_coverage.py` — the structural proof that closes architecture §1.7's latent bypasses (`byteplus_ark.py`, `higgsfield.py`, `veo_official.py`, `runway_official.py`, `kling_official.py` either wired through the gate or explicitly quarantined under a path this test asserts has no production importer). This commit is the one that lets architecture doc's Success Criteria be marked met — for the video domain only; Commits 1-10 repeat as separate waves for image, voice/music, text/LLM, and research per architecture §8.2 step 10.

Commit 1 is the only one authorized to begin without further discussion,
since it is purely additive, zero-behavior-change, and fully covered by
its own tests. Commits 2 onward each require the prior commit's tests
green and, from Commit 4 onward, real `"shadow"`-mode production
observation before proceeding — this blueprint does not authorize
skipping that observation window to move faster.
