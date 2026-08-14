# Authorized Generation Envelope & Mandatory Submission Gate

Status: **DESIGN COMPLETE — NOT YET IMPLEMENTED**

This document is architecture and specification only. No runtime code
implementing this design exists in the repository as of this document's
creation. See `AUTHORIZED_GENERATION_ENVELOPE_IMPLEMENTATION_BLUEPRINT.md`
for the bounded commit sequence that will implement it.

Scope: Cortex Factory Core. Generic, channel-agnostic. Hypogeum is the
validation customer whose current repository state (traced below) is the
evidence base for this design, not its subject.

Trigger: a fail-closed batch preflight (`tools/muapi_batch_preflight.py`)
found that all 88 of Hypogeum's active authorized video generations for
episode `ep_f3619022` carried stale, pre-fix prompt material — the
authorization manifest's stored prompt snapshot had silently diverged
from what the (subsequently fixed) compiler would now produce. That
specific defect was repaired (`fix(runtime): recompile stale authorized
shot prompts`, commit `5569cfd`). This document specifies the permanent,
structural invariant that makes the *entire class* of defect — any paid
generation reaching a provider on stale, unverified, or scene-dominated
material — impossible by construction, for every paid-generation domain,
not only video.

---

## Phase 1 — Paid-Entry-Point Inventory

Traced by direct repository inspection (grep for provider HTTP calls,
`^def submit_*`/`^def generate_*` definitions, and their actual callers)
on 2026-08-14 at HEAD `5569cfda1340e1dcdbfef2cd1ccb82cd38b55089`. This is
the evidence base the rest of this document designs against.

### 1.1 Video generation

| Entry point | Caller(s) | Can spend | Requires authorization today | Retry | Fallback | Resume | Idempotency | Budget enforcement | Bypass risk |
|---|---|---|---|---|---|---|---|---|---|
| `hypogeum/muapi.py::submit_scene()` | `hypogeum/agents/video_generation_agent.py` (first-pass VPD generation) | Yes | **No** — no authorization-manifest concept in first-pass generation at all | Yes, internal (`_attempt_generation`, tier fallback) | Yes (`MUAPI_ALLOW_PAID_FALLBACKS`, T2→T3) | Via `asset_index` skip on `generation_status in {generated, reused, dry_run, deferred}` | Scene-id-keyed skip check, not a true consumed-once ledger | `MUAPIBudgetGuard` (episode/scene credits, `max_paid_attempts`), caller-supplied | **Confirmed deepest video chokepoint** for this call path; no authorization gate upstream of it |
| `hypogeum/muapi.py::submit_scene()` | `hypogeum/agents/clip_repair_agent.py::fill_coverage_deficits()` | Yes | No (deficit-driven, not manifest-authorization-driven) | Yes, internal | Yes | Slot-based (`_next_free_extra_slot`) | Slot occupancy check | `MUAPIBudgetGuard` | Same chokepoint, same gap |
| `hypogeum/muapi.py::submit_scene()` | `hypogeum/agents/clip_repair_agent.py::fill_confirmed_missing_shots()` | Yes | **Partial** — caller pre-vets shot IDs via `hypogeum.shot_compatibility_policy`, but no content-addressed envelope; resumability keys on `planned_shot_id` in `asset_manifest`, not a consumed-once authorization | Yes, internal | Yes | Yes (resumability check before submit) | `manifest_entry_by_shot_id` lookup, not atomic | `MUAPIBudgetGuard(max_paid_attempts=1)` | Closest existing approximation of authorization, but the "authorization" (`generation_authorization_manifest_*.json`) is a **file the caller constructs and passes in**, not something the gate itself verifies |
| `hypogeum/muapi.py::submit_scene()` | `tools/muapi_canary_submit.py` | Yes | **Yes** — 13 fail-closed checks, but implemented as an optional CLI preflight the caller chooses to run, not something `submit_scene()` itself enforces | No (explicit no-retry design) | No | N/A (single-shot) | Output-path-exists check | `MUAPIBudgetGuard(max_paid_attempts=1)`, `--max-charge-usd` | Best current practice, but bypassable — nothing stops a caller from constructing a `scene_spec` and calling `submit_scene()` directly, skipping the CLI entirely |
| `hypogeum/byteplus_ark.py::submit_video_task()` | none (test-only: `tests/test_byteplus_ark.py`) | Yes | No | Own retry primitives (dormant) | N/A | N/A | N/A | None | **Latent bypass**: a fully-functional, independently HTTP-capable video-generation client with zero wiring into any agent, zero authorization awareness. Nothing in the repository prevents a future caller from importing it directly. |
| `hypogeum/higgsfield.py::submit_dop_generation()` | none in production (`scripts/compare_higgsfield_vs_muapi.py` benchmark script; `_archive/stale-untracked-2026-07-10/...` archived tool; tests) | Yes | No | N/A | N/A | N/A | N/A | None | **Latent bypass**, same class as above |
| `hypogeum/veo_official.py`, `hypogeum/runway_official.py`, `hypogeum/kling_official.py` | `probe_credential()` only (credential validation, not generation) called from `hypogeum/provider_verifier.py` | Credential probes only; no generation function is called from production today (`runway_official.py`/`kling_official.py` do not even define a submission function yet; `veo_official.py` additionally defines `list_veo_models`) | N/A (no generation path yet) | N/A | N/A | N/A | N/A | None | Same latent-bypass class if a generation function is ever added here without being routed through the gate |
| `hypogeum/providers/muapi_video_upscaler.py` (via `hypogeum/providers/enhancement.py::get_enhancement_provider()`) | `hypogeum/agents/clip_repair_agent.py` (`qualify_asset()` / `require_source_qualification` path) | Yes | No manifest authorization; gated only by `upscale_budget_credits` derived from `authorize_compliant_shot_generation()` | Provider-internal | No | Skips if already-compliant (measured, not trusted) | None beyond the compliance check itself | Caller-supplied `upscale_budget_credits` | Same class as `submit_scene()`: real spend, no envelope, no gate |

### 1.2 Image generation (reference images + thumbnails)

| Entry point | Caller(s) | Notes |
|---|---|---|
| `hypogeum/muapi.py::generate_reference_image()` | `hypogeum/agents/image_generation_agent.py` | Same module as `submit_scene()`, distinct function — a second chokepoint inside the same file, not currently unified with video's budget guard or any authorization concept |
| `hypogeum/muapi.py::generate_thumbnail()` → `_generate_thumbnail_one_provider()` → `_generate_thumbnail_with_model()` / `_generate_thumbnail_openai()` / `_generate_thumbnail_openrouter()` | `hypogeum/agents/thumbnail_generation_agent.py` (up to `_MAX_ATTEMPTS_PER_CANDIDATE` retries) → `hypogeum/agents/thumbnail_director.py` | **Multiple internal provider fallback paths in one function** — MUAPI primary, OpenAI, OpenRouter-routed models — each capable of independent spend. No shared authorization envelope across the fallback chain today. |

### 1.3 Voice and music generation

| Entry point | Caller(s) | Notes |
|---|---|---|
| `hypogeum/elevenlabs.py::generate_narration_segment()` | `hypogeum/agents/voice_generation_operator.py` | Own retry/rate-limit handling (`ElevenLabsRateLimitError`, `_retry_delay`); no authorization envelope |
| `hypogeum/elevenlabs.py::generate_music_track()` | `hypogeum/agents/music_agent.py` | Same class |

`audio_agent.py`, `caption_agent.py`, `assembly_agent.py` import only
**result types** (`NarrationSegmentResult`, `MusicTrackResult`) from
`elevenlabs.py` — confirmed not independent callers of the paid
functions.

### 1.4 Paid text/LLM generation (cost-authorization-relevant)

| Entry point | Caller(s) | Notes |
|---|---|---|
| `hypogeum/agent.py::run_agent()` → `hypogeum/openrouter.py::call_claude()` | 11 distinct call sites across `hypogeum/agents/*.py` (Executive Producer, Research Director synthesis, Narrative Architect, Script Engine, Thumbnail Strategy, Cinematic Director batches, etc.) | Has its own internal fallback: `call_claude()` → `_call_anthropic_direct()` (direct Anthropic API, bypassing OpenRouter) when OpenRouter fails and `ANTHROPIC_API_KEY` is set. This is a real, live, in-code provider fallback with its own cost implications, occurring beneath a single shared function — the correct existing precedent for "fallback requires the same chokepoint," but not currently authorization-gated by anything resembling an envelope. |
| `hypogeum/perplexity.py::query_perplexity()` | `hypogeum/agents/research_director.py` | Own retry (`_execute_with_retry`); no authorization envelope |

### 1.5 Explicitly out of scope (traced, confirmed non-generation)

- `hypogeum/publishing/{youtube,tiktok,meta,staging}.py` — upload/publish operations. Not provider generation spend; excluded per this document's own charter (Phase 1 instruction: video/image/voice/music/paid-text generation, retry, fallback, upscaling, paid download/transformation). Publishing is downstream of a *qualified, allocated* asset and carries no per-call provider credit cost in the current provider set.
- `hypogeum/meta_analytics.py`, `hypogeum/notifications.py`, `hypogeum/credential_manager.py`, `hypogeum/provider_comparison.py`, `hypogeum/provider_verifier.py` — analytics, notification, and credential-probe HTTP calls. Credential probes (`probe_credential()`) contact providers but do not generate billable content; still recommended (Phase 5 gate item 20) to remain outside the gate's spend-authorization scope but subject to the same "no provider call from preflight/dry-run code" discipline already enforced in `hypogeum/canary_preflight.py` and `tools/muapi_batch_preflight.py`.

### 1.6 Confirmed deepest shared boundaries (one per paid domain — never a single global chokepoint)

Repository evidence does **not** support a single universal chokepoint.
There are five independent paid domains, each with its own deepest
shared function:

| Domain | Deepest shared boundary |
|---|---|
| Video generation | `hypogeum/muapi.py::submit_scene()` |
| Reference/thumbnail image generation | `hypogeum/muapi.py::generate_reference_image()` / `generate_thumbnail()` |
| Video upscaling | `hypogeum/providers/enhancement.py::get_enhancement_provider()` result's `.enhance()`-family call |
| Voice/music generation | `hypogeum/elevenlabs.py::generate_narration_segment()` / `generate_music_track()` |
| Paid text/LLM generation | `hypogeum/agent.py::run_agent()` |
| Paid research (Perplexity) | `hypogeum/perplexity.py::query_perplexity()` |

The mandatory submission gate (Phase 5) must therefore be a **generic
function each of these six boundaries calls**, not a modification
confined to `muapi.py`. Phase 10's migration order sequences video first
(highest current risk, richest existing tooling to build from) and
treats the other five as later, structurally identical migrations of the
same gate.

### 1.7 Confirmed latent bypasses (must be closed, not merely documented)

`hypogeum/byteplus_ark.py`, `hypogeum/higgsfield.py`,
`hypogeum/veo_official.py`, `hypogeum/runway_official.py`,
`hypogeum/kling_official.py` are independently network-capable,
zero-authorization video-generation clients with no current production
caller. They are not a bypass *today* only because nothing calls them in
production. The migration plan (Phase 8) requires these to either be
(a) wired to call through the same gate as `submit_scene()` before any
production caller is added, or (b) explicitly quarantined (e.g. moved
under a `dormant_providers/` path requiring an explicit, documented
promotion step) so a future caller cannot reach a provider through them
by accident. This document treats them as in-scope future chokepoints,
not out-of-scope dead code.

---

## Phase 2 — Canonical Execution Flow

```text
Channel Package + structured plan + runtime policy
        │
        ▼
canonical compiler
        │
        ▼
immutable Generation Execution Envelope
        │
        ▼
authorization decision
        │
        ▼
mandatory submission gate  ◄── every domain boundary in §1.6 calls this, not the provider directly
        │
        ▼
provider adapter
        │
        ▼
immutable provider receipt
        │
        ▼
validation
        │
        ▼
registration
        │
        ▼
allocation
        │
        ▼
learning evidence
```

**Provider adapter contract.** A provider adapter (the thin function that
actually performs `requests.post`/SDK call) receives exactly one
argument: a validated, gate-approved envelope payload. It:

- MUST NOT look up, reconstruct, amend, prepend, append, shorten,
  enhance, or independently retrieve a prompt after authorization —
  every field it transmits comes verbatim from the envelope.
- MUST NOT change model, duration, aspect ratio, seed, or output path
  from what the envelope specifies.
- MUST NOT retry or fall back to a different model/provider on its own
  initiative — a retry or fallback is a *new* gate check against a
  *separately authorized* envelope field (§5, item 18), never an
  adapter-local decision.
- MUST NOT exceed the envelope's authorized cost ceiling.
- Returns a provider receipt (§6) and nothing else; it does not decide
  whether the result qualifies, is registered, or is allocated — those
  are downstream stages.

This mirrors the isolation `tools/muapi_canary_submit.py` already
demonstrates for one call path (preflight → exactly one `submit_scene()`
call → receipt), generalized to every domain and made structurally
mandatory rather than a CLI convention.

---

## Phase 3 — Generation Execution Envelope

A generic, versioned, content-addressed data model. One shape, reused by
every paid domain in §1.6 — the envelope's `provider_operation` field
(§3.2) is what varies, not the schema.

### 3.1 Field list

| Field | Immutable after `authorized`? | Notes |
|---|---|---|
| `envelope_schema_version` | Yes | e.g. `"1.0"` |
| `envelope_id` | Yes | Content-addressed: `sha256` of the canonical JSON of every field below except `envelope_id` itself and audit/timestamp fields |
| `envelope_version` | Yes (a new mutation produces a new `envelope_id` + incremented `envelope_version`, never an in-place edit) | Monotonic integer per logical shot/work-item lineage |
| `organization_id` | Yes | Present for future multi-tenant Cortex; `null`/omitted is valid for a single-tenant deployment |
| `channel_id` | Yes | e.g. `"hypogeum"` |
| `channel_package_version` | Yes | |
| `channel_package_checksum` | Yes | sha256 of the compiled Channel Package used |
| `episode_id` / `execution_id` / `work_item_id` | Yes | Whichever identity the Workflow Model's Execution entity uses for this run (Cortex terminology — see `EXECUTION_MODEL.md`) |
| `workflow_version` | Yes | Registered Workflow version (per `WORKFLOW_MODEL.md`'s Workflow Registry) |
| `plan_version` | Yes | |
| `plan_checksum` | Yes | sha256/md5 (schema-defined, one algorithm, never mixed) of the canonical structured plan source |
| `scene_id` | Yes | |
| `beat_id` | Yes | Omitted for domains with no beat concept (e.g. a standalone thumbnail envelope may carry `null`) |
| `planned_shot_id` (or equivalent work-unit ID for non-video domains) | Yes | |
| `structured_spec_checksum` | Yes | sha256 of the canonical JSON of the structured planned-item specification (the exact input the compiler consumed) |
| `compiler_identity` | Yes | Fully-qualified callable path, e.g. `hypogeum.video_prompt_writer.compile_authoritative_shot_prompt` |
| `compiler_version` | Yes | Git commit hash (or release tag once Cortex is packaged) of the compiler at compile time |
| `provider_bound_content` | Yes | The exact UTF-8 string (or structured payload for non-text domains) submitted to the provider |
| `provider_bound_content_sha256` | Yes | sha256 of the exact bytes above |
| `content_provenance` | Yes | `{compiler, compiler_version, source_work_item_id, rendered_at}` |
| `provider` | Yes | e.g. `"muapi"`, `"elevenlabs"`, `"openrouter"` |
| `model` | Yes | |
| `provider_operation` | Yes | e.g. `"video.text_to_video"`, `"image.reference"`, `"voice.narration_segment"`, `"video.upscale"`, `"text.completion"` |
| `duration_seconds` | Yes (when applicable) | `null` for domains without a duration concept |
| `aspect_ratio` | Yes (when applicable) | |
| `seed` | Yes (when applicable) | `null` when the provider has no seed concept |
| `deterministic_output_path` | Yes | |
| `expected_output_identity` | Yes | What downstream registration expects to find (asset_id shape, checksum-of-nothing placeholder, etc.) |
| `authorization_id` | Yes | |
| `authorization_version` | Yes | |
| `authorization_status` | **No** — this is the one deliberately mutable field, transitioning per §4 | |
| `max_cost_per_call` | Yes | |
| `run_level_budget_ref` | Yes | Reference to the budget scope (§7), not a copy of its remaining balance |
| `episode_level_budget_ref` | Yes | |
| `currency` | Yes | e.g. `"USD"` |
| `retry_authorized` | Yes | boolean; if true, `retry_budget_ref` must be present |
| `fallback_authorized` | Yes | boolean; if true, `fallback_model`/`fallback_provider` and a separate budget reservation must be present |
| `idempotency_key` | Yes | Deterministic function of `(authorization_id, authorization_version, provider_operation)` — never a random UUID, so a crash-and-retry of the *same* authorized call reproduces the *same* key |
| `created_at` | Yes | |
| `expiration_policy` | Yes | `{expires_at | never, on_expiry: "invalidated_source_changed"}` |
| `dependency_checksums` | Yes | `{channel_package, plan, structured_spec, compiler}` — the exact set the gate re-verifies (§5) |
| `superseded_envelope_id` | Yes (set once, at creation, never edited later) | `null` for a first-generation envelope; points at the prior envelope this one replaces when set by §4's invalidation flow |
| `audit_metadata` | Yes | `{created_by, creation_reason}` — free-form, never interpreted by the gate |

### 3.2 What must never live in Factory Core

Per `CHANNEL_PACKAGE_SPEC.md`, the envelope references a **compiled
Channel Package version/checksum** and a **task-specific context
snapshot** (the structured spec, the compiled prompt) — it never embeds
channel creative identity (Visual DNA, Prompt DNA, brand vocabulary)
inline as Factory Core schema. The envelope schema itself has zero
Hypogeum-specific, archaeology-specific, or beat/shot-ID-convention
fields; `planned_shot_id`, `beat_id`, `scene_id` are generic identity
slots whose *values* happen to look like `"b_047__shot3"` for Hypogeum
and would look entirely different for a different Channel Package.

---

## Phase 4 — Authorization Lifecycle

### 4.1 States

```
draft
  → compiled
      → preflight_failed  [terminal]
      → awaiting_authorization
          → authorized
              → submitting
                  → submitted
                      → downloaded
                          → technical_validation_failed  [terminal]
                          → pending_editorial_review
                              → qualified
                                  → allocated
                                      → consumed  [terminal]
                              → rejected  [terminal]
          → invalidated_source_changed  [terminal — see §4.4]
          → cancelled  [terminal]
      → superseded  [terminal — see §4.4]
```

`awaiting_authorization`, `authorized`, and `submitting` may each also
transition directly to `invalidated_source_changed` or `cancelled` if a
dependency changes or an operator cancels before submission.

### 4.2 Ownership per transition

| Transition | Owner |
|---|---|
| `draft → compiled` | Compiler invocation (the canonical compiler for that domain, §2) |
| `compiled → preflight_failed` / `→ awaiting_authorization` | The deterministic preflight (domain-specific `*_batch_preflight.py`-class tool or its future generic successor) |
| `awaiting_authorization → authorized` | Authorization decision — human editorial approval (Canary-class flows) or automated policy approval (batch/scheduled flows operating within pre-approved budget and distinctness thresholds) |
| `authorized → submitting` | The mandatory submission gate, immediately before the provider call, and only after all 20 checks (§5) pass |
| `submitting → submitted` | The provider adapter, on receiving a provider acceptance (job ID or equivalent) |
| `submitting → <unresolved>` | Crash/timeout recovery (§6.2) — never auto-advances to `submitted` on ambiguous provider acceptance |
| `submitted → downloaded` | The download/validation stage |
| `downloaded → technical_validation_failed` / `→ pending_editorial_review` | The technical validator (deterministic, provider-agnostic) |
| `pending_editorial_review → qualified` / `→ rejected` | Human editorial verdict (never automated — mirrors the existing `APPROVE_QUALIFIED` / `REJECT_NO_RETRY` Canary convention) |
| `qualified → allocated` | The allocation step (coverage-deficit resolution, deliverable assignment) |
| `allocated → consumed` | Registration of the allocation as permanently fulfilling its authorization |
| `* → invalidated_source_changed` | The dependency-watch mechanism (§4.4), automatic, never manual |
| `* → superseded` | Creation of a new envelope version that supersedes this one (§4.4) |
| `* → cancelled` | Explicit operator/policy cancellation only |

### 4.3 Idempotency, recovery, and double-submission prevention

- **Idempotency key** (§3.1) is deterministic from `(authorization_id,
  authorization_version, provider_operation)`. The gate (§5, item 5)
  refuses any submission whose idempotency key has already been recorded
  as consumed in the write-ahead receipt store (§6), regardless of
  process identity — this is what makes recovery-after-crash safe
  without relying on in-memory state.
- **Recovery after process death**: on restart, any envelope left in
  `submitting` with no corresponding receipt entry is re-verified against
  the write-ahead log (§6.2) before any action is taken — never
  auto-resubmitted.
- **Unknown provider acceptance** (request sent, response never received):
  the envelope stays in `submitting`. The gate's write-ahead log (§6.2)
  requires an explicit reconciliation step (poll the provider for a job
  matching this idempotency key, if the provider supports that; otherwise
  requires a human/operator decision) before the envelope may transition
  further. It may never silently retry.
- **Output downloaded but registration failed**: envelope stays in
  `submitted`; the physical asset is preserved (never deleted); the
  registration step is safe to retry because it is itself idempotent
  (keyed on `deterministic_output_path`, matching the existing convention
  in `hypogeum/muapi.py::scene_file_path()`).

### 4.4 Source-change invalidation (never in-place repair)

Any mutation to: Channel Package, structured scene/shot specification,
plan, compiled prompt, prompt compiler, provider routing, model,
duration, aspect ratio, seed, deterministic output path, or price policy
— **automatically invalidates every unconsumed envelope whose
`dependency_checksums` no longer match the live source.**

The system:

1. Marks the affected envelope/authorization `invalidated_source_changed`.
2. Records the exact changed dependency (which checksum, old value, new
   value) in that envelope's immutable audit trail.
3. Recompiles through the canonical compiler for that domain.
4. Reruns the deterministic preflight and distinctness validation for the
   new candidate.
5. Creates a new envelope (`envelope_version` incremented,
   `superseded_envelope_id` pointing at the old one) and a new
   authorization version.
6. Preserves the superseded envelope/authorization record immutably — it
   is never deleted, never edited, and its own `authorization_status`
   becomes `superseded`, a terminal state.

**No process may update a stale envelope's checksum field in place to
make it appear current.** This is the exact failure class this whole
document exists to close (the 88-entry Hypogeum incident was precisely a
manifest whose checksum field was never invalidated when its source
compiler changed underneath it). §5 item 6-10 structurally enforces this
by re-verifying every checksum against *live* source immediately before
spend, not by trusting whatever the envelope claims about itself.

---

## Phase 5 — Mandatory Submission Gate

A single generic function, `verify_and_consume_envelope(envelope) ->
GateResult`, called by every domain boundary in §1.6 immediately before
its provider adapter call. Returns `GateResult(approved=False, reason,
failed_check)` on the first failing check — **no check after the first
failure runs**, and no network access has occurred by that point.

| # | Check | What "failure" means |
|---|---|---|
| 1 | Envelope schema is supported | `envelope_schema_version` is a version this gate build knows how to verify |
| 2 | Envelope is complete and content-addressed correctly | Every required field (§3.1) present; recomputed `envelope_id` matches the stored one |
| 3 | Authorization is active and unconsumed | `authorization_status == "authorized"` exactly (not `consumed`, not any terminal state) |
| 4 | Authorization and envelope versions match | `authorization_version == envelope.authorization_version` |
| 5 | Idempotency key has not already been consumed | Lookup in the write-ahead receipt store (§6) |
| 6 | Channel Package checksum matches | Recompute/read live Channel Package checksum; compare to `dependency_checksums.channel_package` |
| 7 | Plan checksum matches | Same pattern, live plan source |
| 8 | Structured-shot checksum matches | Same pattern, live structured spec |
| 9 | Compiler identity/version matches | Live compiler's own version marker equals `compiler_version` |
| 10 | Exact prompt checksum matches | Recompute sha256 of `provider_bound_content`; compare to `provider_bound_content_sha256` — this is `hypogeum.canary_preflight.check_prompt_checksum()`'s exact check, generalized |
| 11 | Prompt provenance is valid | `content_provenance` has non-empty `compiler` and `source_work_item_id` — this is `hypogeum.canary_preflight.check_provenance()`'s exact check, generalized |
| 12 | Per-shot direction is authoritative | For domains with a scene/shot split: the compiled content contains its own authoritative marker (the exact structural invariant `tools/muapi_batch_preflight.py`'s `scene_level_prompt_dominance_leak` check already verifies) |
| 13 | No scene-level prompt dominance leak | Same audit `tools/muapi_batch_preflight.py::_run_distinctness_audit()` performs, reused not reimplemented |
| 14 | Model/provider/operation match authorization | Exact string equality against the envelope's own `provider`/`model`/`provider_operation` — this is `hypogeum.canary_preflight.resolve_model_and_cost()`'s model-match check, generalized |
| 15 | Duration/aspect ratio/seed/output identity match | Exact equality against authorization-time values |
| 16 | Output does not already exist as unrelated or completed media | The `scene_file_path()`-style collision check `plan_confirmed_shot_allocations()` and `tools/muapi_batch_preflight.py::_already_registered_shot_ids()` already implement, generalized |
| 17 | Per-call, run-level, episode-level budgets permit the call | `MUAPIBudgetGuard`'s existing check, generalized to the budget scopes in §7 |
| 18 | Retry and fallback are separately authorized | If this call is a retry/fallback attempt, a *distinct* envelope field (`retry_authorized`/`fallback_authorized`) must be true and reference its own budget reservation — never inherited implicitly from the original attempt |
| 19 | Runtime lease/ownership prevents concurrent duplicate execution | A lease record (owner process/worker ID + expiry) must be acquired atomically before check 20; prevents two workers from both passing checks 1-18 for the same envelope |
| 20 | Audit persistence is available before spend | The write-ahead log (§6.2) must successfully record "about to submit, idempotency key X" *before* the network call — if this write fails, the gate refuses, not the adapter |

**No bypass.** `--force`, a "legacy mode," a repair path, an internal
function call, a scheduler, or a recovery worker may not skip this gate.
The only sanctioned bypass is a **fake provider adapter incapable of
network access**, used exclusively by tests — the gate itself still runs
in full against the fake adapter; only the final HTTP/SDK call is
replaced.

---

## Phase 6 — Provider Receipt & Crash Recovery

### 6.1 Immutable provider-operation receipt

| Field | |
|---|---|
| `envelope_id` / `envelope_version` | |
| `authorization_id` / `authorization_version` | |
| `idempotency_key` | |
| `provider` / `model` / `provider_operation` | |
| `request_checksum` | sha256 of the exact bytes sent |
| `submitted_content_checksum` | Equals `envelope.provider_bound_content_sha256` — recorded again here so the receipt is independently verifiable without cross-referencing the envelope |
| `provider_job_id` | |
| `provider_asset_id` | |
| `call_count` | Always `1` per receipt — a retry is a new receipt referencing a new idempotency key, never a mutated count on this one |
| `charge_actual_or_estimated` | With an explicit `is_estimated: bool` flag — never silently conflated |
| `submitted_at` | |
| `response_checksum` | sha256 of the raw provider response body |
| `download_source` | |
| `output_checksum` | sha256 of the downloaded artifact, once available |
| `status_history` | Append-only list of `{status, at, evidence}` |
| `recovery_evidence` | Populated only when a crash-recovery reconciliation occurred; `null` otherwise |

### 6.2 Write-ahead sequence

```
1. Gate check 20 passes → write receipt row, status="about_to_submit",
   idempotency_key recorded (this write itself is the durability
   boundary — if it fails, refuse before network access)
2. Provider adapter performs the network call
3a. Success + job ID received → append status="accepted", provider_job_id
3b. Timeout/no response → append status="unknown_acceptance"
4. Poll/confirm → append status="submitted" (job confirmed to exist) or
   status="rejected" (provider explicitly rejected)
5. Download → append status="downloaded", output_checksum
6. Registration → append status="registered"
```

### 6.3 Crash scenarios and required behavior

| Scenario | Required behavior |
|---|---|
| Crash before request | Receipt row exists at `about_to_submit`, no provider contact occurred. Recovery: safe to resume from the gate (idempotency key unconsumed at the provider). |
| Crash during request | Receipt row at `about_to_submit`; provider acceptance unknown. Recovery: reconciliation required (§4.3) — never blind retry. |
| Timeout, unknown acceptance | Same as above — `status_history` records `unknown_acceptance` explicitly. |
| Crash after provider acceptance, before job-ID persistence | The write-ahead write in step 3a must itself be durable before the adapter returns control — if this is impossible for a given provider's response timing, the adapter must poll for its own just-submitted job by idempotency key (if the provider supports querying by client-supplied key) before returning, or the gate must treat this as `unknown_acceptance`. |
| Crash during polling | Resume polling using the persisted `provider_job_id`; never resubmits. |
| Crash during download | Resume download using `download_source`; the eventual `output_checksum` is what proves completeness, not the presence of a file. |
| Crash before output registration | Physical asset preserved; registration is idempotent (keyed on deterministic output path) and safe to retry. |
| Runtime restart | Every envelope in a non-terminal state is re-evaluated against its own receipt's `status_history` before any further action — never assumed complete, never assumed failed. |

**Unknown provider acceptance must never trigger an automatic blind
retry.** This is a hard invariant, not a default that can be configured
away.

---

## Phase 7 — Budget & Circuit-Breaker Policy

### 7.1 Budget scopes

`call` → `cohort` → `run` → `episode` → `channel` → `organization` →
`time_window`. Each scope is a reference the envelope carries
(`run_level_budget_ref`, `episode_level_budget_ref`, §3.1), never a
balance copied into the envelope itself (which would go stale exactly
like the prompt-checksum incident this document exists to prevent).

### 7.2 Automatic stop conditions

Any one of the following halts the *entire remaining batch/run*, not
just the one call:

- First unauthorized state mismatch (any gate check 3-16 failing)
- First provider failure classified as systemic (mirrors
  `hypogeum.agents.clip_repair_agent._is_billing_contradiction()`'s
  existing pattern, generalized beyond billing text)
- Unknown provider acceptance (§6.3)
- Cost mismatch (actual charge reported by the provider differs from the
  envelope's `max_cost_per_call` in a way that indicates the provider
  billed for something other than what was authorized)
- Repeated technical-validation failures (threshold configurable per
  Channel Package policy, default matches existing
  `hypogeum.artifact_compatibility` conventions)
- Repeated semantic-risk indicators (the `REVIEW_SEMANTIC_RISK`
  classification `tools/muapi_batch_preflight.py`'s distinctness audit
  already produces, thresholded)
- Duplicate-output detection (gate check 16)
- Unexpected model/provider (gate check 14)
- Receipt persistence failure (gate check 20)
- Budget exhaustion at any scope in §7.1

### 7.3 Retry and fallback

Require **explicit policy** (a Channel Package or run-level policy
setting, never an implicit default) **and a separately reserved
budget** distinct from the original attempt's budget — mirrors
`MODEL_CREDIT_COSTS`/`UPSCALE_COST_ESTIMATE_USD`'s existing separation of
generation-cost and upscale-cost budgets in
`hypogeum.agents.clip_repair_agent`, generalized to every domain.

---

## Phase 8 — Migration Plan

### 8.1 Current state (repository evidence, §1)

- Ordinary first-pass generation: no authorization envelope.
- Coverage repair: partial (shot-ID pre-vetting, no envelope).
- Canary CLI: best current practice (13 fail-closed checks), but a
  bypassable convention, not a structural gate.
- Batch preflight: read-only audit, zero enforcement power at submission
  time.
- Existing manifests (`generation_authorization_manifest_*.json`,
  `canary_authorization_*.json`): ad hoc, per-episode, hand-evolved JSON
  shapes with no formal schema version.
- Existing generated media and the current Hypogeum episode state: real,
  valuable, must not be invalidated retroactively by this migration.

### 8.2 Required migration direction

1. Introduce the envelope schema and authorization lifecycle (§3, §4) as
   pure data-model additions — no behavior change yet.
2. Put the gate (§5) at the deepest shared paid boundary **for one
   domain first** (video — richest existing tooling, highest current
   risk per §1.7). Confirmed boundary: `hypogeum/muapi.py::submit_scene()`.
3. Migrate every video caller (§1.1's table) to construct and pass a
   real envelope, one caller per bounded commit (§10).
4. Once every video caller is migrated, make `submit_scene()` **reject**
   any call that does not carry a valid, gate-approved envelope — no
   raw-`scene_spec`-only calling convention survives.
5. Preserve exactly one test-only bypass: a fake provider adapter
   (`FakeProviderAdapter`, network-incapable by construction — e.g. it
   never imports `requests`/`httpx` and raises if asked to perform I/O)
   that the gate still runs its full 20 checks against.
6. Convert legacy manifests (`generation_authorization_manifest_*.json`,
   `canary_authorization_*.json`) into versioned envelopes via an
   explicit, one-time, auditable migration tool (§10) — **without
   claiming they were originally generated under the new invariant**.
   Each converted envelope's `audit_metadata.creation_reason` records
   `"migrated_from_legacy_manifest"` and the source file's checksum, so
   the historical record stays honest.
7. Remove or disable legacy bypass paths (raw `scene_spec` submission,
   the ad hoc manifest format as a *live* authorization source) only
   after step 4's rejection behavior has been live and observed for at
   least one full production cycle without a required exception.
8. Prove complete call-site coverage: a repository-wide test (§10, test
   category "no real provider adapter can be called without a valid
   execution envelope") that greps/imports every module in §1 and
   asserts each one's provider-calling function requires a
   `GateResult(approved=True)` before it can execute a network call.
9. Only then — after steps 1-8 are each independently verified, not
   bundled — is cohort or batch generation for any episode authorized to
   resume.
10. Repeat steps 2-9 for each remaining domain (image, upscale,
    voice/music, text/LLM, research) as separate, independently-bounded
    migration waves — never combined into the video wave's commits.

### 8.3 Compatibility duration and removal criteria

- The legacy manifest format remains **readable** (for migration and
  historical audit) indefinitely — old episodes' superseded records are
  immutable evidence, never deleted.
- The legacy manifest format stops being **accepted as a live
  authorization source** the moment step 8.2.4 ships for a given domain.
  No dual-mode period beyond the single migration wave that converts it —
  this document explicitly rejects "leave both paths available
  indefinitely" as a permanent state.
- Explicit removal criterion for each domain's legacy bypass: zero
  production callers remain (verified by the same call-site-coverage
  proof in step 8), and at least one full production cycle has run
  exclusively through the gate without an emergency exception being
  invoked.

---

## Success Criteria

This design is **implemented**, not merely documented, only when:

- Every entry point in §1.1-§1.4's tables (and §1.7's latent bypasses)
  calls `verify_and_consume_envelope()` before any network access, with
  no exception reachable outside a fake test provider.
- The regression tests specified in the implementation blueprint
  (repository-wide, per entry point) pass.
- A source change (Channel Package, plan, compiler version, price
  policy) is proven — by test, not by manual inspection — to invalidate
  every affected unconsumed authorization automatically.
- The current 88-entry Hypogeum state (or any future equivalent) would
  have been caught **without a human running an audit CLI** — i.e., the
  gate itself would have refused the stale submissions structurally, not
  merely reported them after the fact.

Until every item above is independently verified, this failure class is
**repaired for the current episode, not permanently closed** — see
`CURRENT_STATE.md`'s corresponding entry (Phase 9) for the authoritative
status statement.
