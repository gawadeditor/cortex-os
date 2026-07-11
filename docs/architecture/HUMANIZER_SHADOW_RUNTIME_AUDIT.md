# Humanizer SHADOW-Mode Runtime Audit

**Date:** 2026-07-11
**Audited commit:** `7d04057` — "Add shadow-mode upstream Humanizer evaluation"
**Status:** READ-ONLY AUDIT — no production code or test file was modified to produce this document.
**Method:** Graphify-first orientation (three queries, budgets 1800/1600/1600 — the local graph has no LLM enrichment configured, so results were raw BFS node listings; findings below are grounded by direct reads of every file the task listed, plus targeted greps for exact line numbers and constants), then direct reads of `hypogeum/agents/humanizer_llm_adapter.py`, `hypogeum/agents/humanization_stage.py`, `hypogeum/pipeline.py`, `hypogeum/agent.py`, `hypogeum/openrouter.py`, `hypogeum/finance.py`, `hypogeum/agents/finance_agent.py`, `hypogeum/agents/provider_router.py`, `hypogeum/humanization_checkpoint.py`, `hypogeum/humanization_fingerprint.py`, `hypogeum/prompts/humanization_stage_llm_wrapper.txt`, `tests/test_humanizer_llm_adapter.py`, `tests/test_humanization_stage.py`, `tests/test_humanization_checkpoint.py`, `tests/test_pipeline.py`, `docs/architecture/HUMANIZATION_STAGE_DESIGN.md`, `docs/architecture/HUMANIZER_RUNTIME_MIGRATION_AUDIT.md`.

---

## 1. Current Runtime Call Graph

```
run_pipeline()                                             [hypogeum/pipeline.py]
  ...
  5. Script Engine → script_package                        (unchanged by this feature)
  5b. Humanization Stage — SHADOW, hardcoded                [pipeline.py:611-638]
        _run_humanization_stage(script_package, config, ...)   [pipeline.py:1434]
          load_reusable_humanization_checkpoint(...)            [humanization_checkpoint.py]
            HIT  → return cached humanized_script_package, no calls, no spend
            MISS → run_humanization_stage(mode="SHADOW", config=config)  [humanization_stage.py:155]
                     for each extracted narration beat:
                       _build_deterministic_beat()             (hypogeum.humanizer, no I/O)
                       _build_shadow_beat()                    [humanization_stage.py]
                         call_humanizer_llm(beat_id, text, episode_id, config)
                                                                [humanizer_llm_adapter.py:199]
                           _system_prompt()  → compose_system_prompt(wrapper.txt, SKILL.md)
                                                                (cached after first call)
                           call_claude(model_slug=HUMANIZER_LLM_MODEL_ID, messages=[...])
                                                                [hypogeum/openrouter.py:190]
                             → OpenRouter POST, retry(≤1), Anthropic-direct fallback
                           record_cost(provider="openrouter", call_type="humanization_stage_llm", ...)
                                                                [hypogeum/finance.py:126]
                           parse {"humanized_text": "..."} or raise HumanizerLLMAdapterError
                         _beat_preservation_checks(original_text, llm_text)
                           pass  → beat accepted (status: llm / deterministic_and_llm / unchanged)
                           fail  → beat reverted to deterministic-pass result, fallback_used=True
                     assemble humanized_script_package, validate() against schema
                save_humanization_checkpoint(...)              [humanization_checkpoint.py]
        result carried on PipelineResult.humanized_script_package only
  6. Finance Agent — budget authorization gate               [pipeline.py:640]
        _run_budget_authorization(script_package=script_package, ...)   ← original script_package,
                                                                            no knowledge of stage 5b
  7. Visual Package Director  → script_package (original)
  8. Audio Agent               → script_package (original)
  9. Assembly Agent            → script_package (original)
```

**Key structural fact:** stage 5b sits *between* Script Engine and the Finance Agent's Phase-1 budget-authorization gate (stage 6), and Finance's estimate is computed purely from `script_package` (narration character count, section count) — it has no input path for Humanization Stage's own real spend. This is the basis for Finding D1 below.

---

## 2. `run_agent()` vs. `call_claude()` Comparison (Audit Q1)

`humanizer_llm_adapter.py` calls `hypogeum.openrouter.call_claude()` directly. It does **not** call `hypogeum.agent.run_agent()`, and does not add a new direct provider SDK path — confirmed by reading both files line-by-line and by `tests/test_humanizer_llm_adapter.py::TestIsolation`.

| `run_agent()` guarantee | Inherited by `call_humanizer_llm()`? | Evidence |
|---|---|---|
| Retry (≤1) + Anthropic-direct fallback | **Yes — identical.** `run_agent()` itself adds no retry logic of its own; it delegates entirely to `call_claude()`. Calling `call_claude()` directly gets the exact same retry/fallback behavior. | `hypogeum/agent.py:215-222` calls `call_claude()` with no wrapping retry loop; `hypogeum/openrouter.py:254-311` is the actual retry/fallback implementation, shared by both call sites. |
| Cost recorded via `record_cost()` | **Yes — identical call shape.** Same `provider="openrouter"`, `model=result.model_used`, `cost_usd=result.estimated_cost_usd`, `tokens_or_credits=total_tokens` pattern, called once per successful `call_claude()` return. | `hypogeum/agent.py:226-232` vs `hypogeum/agents/humanizer_llm_adapter.py:227-233`. |
| Structured JSON parsing with markdown-fence stripping | **Reimplemented, not shared.** The adapter has its own `_strip_markdown_fence()` — a byte-identical copy of `hypogeum.agent._strip_markdown_fence()`'s regex, duplicated rather than imported. | `hypogeum/agent.py:122` vs `hypogeum/agents/humanizer_llm_adapter.py:79`. See Finding D3. |
| Schema-registry-validated output (`hypogeum.schemas.validate()`, a name in `SCHEMA_NAMES`) | **Bypassed by design.** The adapter validates only a local, unregistered two-key contract (`{"humanized_text": str}`) via `_parse_humanized_text()`, never touching `hypogeum.schemas`. | `hypogeum/agent.py:268-278` vs `hypogeum/agents/humanizer_llm_adapter.py:190-217`. |
| Durable per-call disk persistence (`_write_package()`) | **Bypassed by design.** No file is written per beat call; the only persisted artifact is the final, whole-stage `humanized_script_package` checkpoint, written once after all beats are processed. | `hypogeum/agent.py:281-282` has no counterpart in the adapter. |
| Uniform `agent_run_started` / `agent_run_completed` / `agent_schema_validation_failed` log-event family | **Not reused — a parallel, private event namespace.** The adapter emits `humanizer_llm_call_started` / `humanizer_llm_call_completed` / `humanizer_llm_call_failed`, structurally similar but a different name family. | `hypogeum/agent.py:198-293` vs `hypogeum/agents/humanizer_llm_adapter.py:236-282`. See Finding D4. |
| A single, well-known exception taxonomy (`ResponseParseError`, `SchemaError`) that calling code and tooling can pattern-match | **Collapsed into one type.** Every failure mode (missing skill, provider failure, malformed output) raises the same `HumanizerLLMAdapterError`, distinguished only by a string prefix (`"provider_failure: ..."`, `"malformed_llm_output: ..."`). | `hypogeum/agents/humanizer_llm_adapter.py:87-101`. Deliberate simplification for a single internal caller (`_build_shadow_beat()`), which only needs a catch-all — not flagged as a defect, but a real scope reduction vs. `run_agent()`'s taxonomy. |

**Architectural verdict on Q1:** Direct `call_claude()` use is **acceptable for the current per-beat call shape**. The two guarantees it genuinely forfeits — schema-registry validation and durable per-call persistence — are guarantees that only make sense for a *stage-level package producer* (one call → one canonical artifact), which a single-beat call is not; forcing every beat through `run_agent()` would mean registering a throwaway schema and writing (and immediately discarding the significance of) dozens of tiny JSON files per episode. That reasoning is sound. However, this decision does create a **second, parallel "call an LLM and track it" idiom** in the codebase with no shared abstraction beneath `call_claude()` itself — any future per-item LLM pass (e.g. a hypothetical per-scene or per-shot refinement stage) would likely reinvent this same pattern a third time, including re-duplicating fence-stripping and reinventing its own log-event names. This is a **refactor-later observation, not a blocking defect** (see §12, Non-Defect Observations, and §13 for the recommended next step).

---

## 3. Finance Authorization and Cost-Accounting Findings (Audit Q2)

**Is every Humanizer model call recorded exactly once?** Yes. `call_humanizer_llm()` calls `record_cost()` exactly once per successful `call_claude()` return (`hypogeum/agents/humanizer_llm_adapter.py:227`), before JSON parsing — matching `run_agent()`'s own ordering (cost is billed regardless of whether the response can subsequently be used). No test or code path calls `record_cost()` twice for one API call.

**Is any cost recorded twice?** No. Confirmed by reading `openrouter.py`'s retry path (`_call_openrouter`, lines 384-451): all `RETRYABLE_STATUS_CODES = {429, 500, 502, 503}` (`openrouter.py:52`) are raised **before** a response body is parsed — no tokens are generated or billed on a retried attempt. `RETRY_MAX = 1` (`openrouter.py:45`) means at most one retried HTTP attempt per beat, and only the final, successful `CallResult` ever reaches `record_cost()`.

**Is the call authorized before spend?** **No.** This is the audit's most significant finding.

**Does SHADOW evaluation currently pass through the Finance Gate, or only record cost after execution?** **Only after execution — there is no gate at all for Humanization Stage's own spend.** Finance Agent's Phase 1 budget-authorization gate (`hypogeum/agents/finance_agent.py:6-10`, "Called after Script Engine, before Visual Package Director") is wired in `pipeline.py` as stage 6, running **after** Humanization Stage (5b). By the time Finance computes its `AUTHORIZED` / `WARNING` / `BLOCKED` verdict, every beat's Humanizer LLM call — if not checkpoint-reused — has already executed and already been billed via `record_cost()`. Finance's estimate (`_estimate_narration_chars()`, `_estimate_section_count()`, both in `finance_agent.py:85-105`) is computed purely from `script_package` content and contains no term for Humanization Stage's LLM spend at all — Humanizer cost is neither gated in advance nor included in the forward estimate.

**Can an autonomous episode exceed budget because Humanizer calls occur before or outside authorization?** Yes, in two distinct ways:
1. **Sunk, ungated spend on a blocked episode.** If Finance later returns `BLOCKED`, every SHADOW-mode Humanizer call for that episode already fired and was billed — the gate protects every later stage's spend but not this one's.
2. **Estimate under-reports actual cost.** Even on an `AUTHORIZED` verdict, Finance's own forward-looking number does not include what SHADOW mode already spent, so an operator reading the authorization record sees an estimate that is silently incomplete by whatever the Humanizer calls actually cost (see §5 for the per-episode dollar range — roughly $0.69–$2.78 in *repeated system-prompt overhead alone* at 25–100 beats, before per-beat content/output tokens).

The actual total spend is still **correctly and completely recorded** in the cost ledger (`write_episode_rollup()` sums every `costs` row regardless of call_type) — this is a gating/estimate-accuracy defect, not a lost-money or double-counted-money defect.

**Model-call-count formula (from the live implementation):** one `call_humanizer_llm()` invocation per narration beat with non-empty text, extracted by `hypogeum.agents.humanization_stage._extract_beats()` (sections then shorts_scripts, duplicates and malformed entries skipped). Formally: `calls = |{beat : beat.text.strip() != "" }|` after de-duplication by `beat_id`. Empty-text beats and checkpoint cache hits make zero calls.

---

## 4. Call Granularity (Audit Q3)

**Confirmed: exactly one model call per narration beat.** `_build_shadow_beat()` (`hypogeum/agents/humanization_stage.py`) calls `call_humanizer_llm()` once per beat, with no batching of any kind. Verified by direct read and by `tests/test_humanization_stage.py::TestShadowMode::test_shadow_mode_invokes_mocked_model_path` (`mock_llm.assert_called_once()` for a one-beat fixture).

| Narration beats | Model calls per episode | Approx. repeated system-prompt tokens (see §5) | Approx. system-prompt-only cost (Sonnet 4.6, $3/M input) |
|---|---|---|---|
| 25 | 25 | ~231,450 | ~$0.69 |
| 50 | 50 | ~462,900 | ~$1.39 |
| 75 | 75 | ~694,350 | ~$2.08 |
| 100 | 100 | ~925,800 | ~$2.78 |

(Figures exclude per-beat user-content tokens and completion tokens, which are small relative to the repeated system prompt — see §5.)

**Timeout implications:** each call uses `openrouter.py`'s shared `REQUEST_TIMEOUT = 120` seconds (`openrouter.py:50`, "generous for long Opus calls") — generous for a single short beat, so timeout false-positives are unlikely per call, but the *aggregate* worst case (many beats each hitting a slow retry+fallback path) is a real, un-bounded wall-clock exposure: nothing in the stage caps total elapsed time across all beats.

**Retry implications:** `RETRY_MAX = 1` per beat (not per episode), so a systematically failing provider produces up to `2 × beats` HTTP attempts, each adding exponential backoff (`RETRY_INITIAL_DELAY=5.0 × RETRY_MULTIPLIER=2.0^attempt`, capped at `RETRY_MAX_DELAY=30.0`). As established in §3, none of this multiplies cost (retried attempts are never billed).

**Rate-limit implication — a genuine amplification, not a new defect class:** `openrouter.py`'s `_use_anthropic_for_run` fallback flag (`openrouter.py:66`) is **module-level, process-lifetime, and never reset by `pipeline.py`** — `reset_run_state()` exists and its own docstring says "Call once at the start of each pipeline run" (`openrouter.py:69-79`), but a repo-wide grep confirms `pipeline.py` never calls it. This predates SHADOW mode. What SHADOW mode changes is the *volume* of same-model sequential calls fired in a tight loop within one stage (up to 100+ in the measured range) — this materially raises the probability of tripping OpenRouter rate limits or hitting a transient outage that exhausts the one retry, which flips `_use_anthropic_for_run = True` for the **remainder of the process**, silently rerouting every subsequent stage's LLM calls (VPD, TD, Audio, and — in `run_worker.py`'s long-running loop — every later episode too) to Anthropic-direct. Flagged as Finding D6.

**Partial-failure implication:** confirmed already fully isolated per beat — one beat's provider failure, malformed output, or preservation-check failure never affects any other beat's result (see §7).

### Batching feasibility

Batching narration beats into fewer calls **is architecturally possible while preserving every listed guarantee**, and the codebase already has a working precedent to model it on: `hypogeum/agents/visual_package_director.py::_compute_a2_batches()` — token-budget-aware batch splitting with per-batch checkpoint files (`vpd_a2_scenes_batch_{n}_{episode_id}.json`) and per-batch resume.

A batched design would:
- Send `[{"beat_id": ..., "text": ...}, ...]` as the user content for a group of beats, and require the model to return `{"results": [{"beat_id": ..., "humanized_text": ...}, ...]}` — **beat_id explicit in both directions**, so re-association is by key, never by list position (protects order and beat_id even if the model reorders or drops an item).
- Preserve **original narration** trivially: the stage already holds the true original text independent of any batched response.
- Preserve **individual fallback and individual preservation checks** unchanged: after a batch response is parsed, each returned item is still run through the existing per-beat `_beat_preservation_checks()` and still independently reverts to the deterministic-pass result on failure. A beat whose `beat_id` is *missing* from the response (partial/truncated batch) simply falls back for that beat alone, exactly like a `HumanizerLLMAdapterError` does today.
- Require **structured output validation** at two levels: the batch envelope shape, then each item's shape — a small, mechanical extension of the existing `_parse_humanized_text()` validator.

**Recommended batch size (not implemented):** 8–15 beats per call. Rationale: the current `_MAX_TOKENS = 2,048` was sized for one beat's rewrite; a batch's required completion budget scales roughly linearly with batch size (each beat's rewrite is typically comparable in length to its input, plus JSON structural overhead), so 8–15 keeps the completion budget in the same low-thousands-of-tokens range that has already been validated, while cutting the *dominant* cost driver — the ~9,258-token repeated system prompt — by the same 8–15× factor. This mirrors VPD's own batching philosophy (bounded batch size, token-budget-ratio guard, never an empty batch) rather than inventing a new pattern. **Batching is a recommendation only; it is not implemented by this audit or this task.**

---

## 5. Prompt and Skill Loading (Audit Q4)

**Pinned SKILL.md loaded verbatim:** confirmed. `_load_system_prompt()` reads `third_party/skills/humanizer/1b48564898e999219882660237fde01bf4843a0f/SKILL.md` via `hypogeum.prompt_loader.load_skill()`/`compose_system_prompt()` and re-verified in this audit by direct SHA-256 comparison against `config/upstream_skills_manifest.json`:

```
actual:   243aecdafecb5e11c2d45e2e088b7876e3f6eee34aa50c53f624d8468039afa8
expected: 243aecdafecb5e11c2d45e2e088b7876e3f6eee34aa50c53f624d8468039afa8
MATCH
```

**No duplication or paraphrase in the wrapper:** confirmed by direct read of `hypogeum/prompts/humanization_stage_llm_wrapper.txt` — it references the upstream rules ("The Humanizer skill definition follows below this line, unmodified") but contains none of the upstream's 33 numbered pattern sections itself. `tests/test_humanizer_llm_adapter.py::TestPinnedSkillIntegrity::test_no_duplicate_or_rewritten_humanizer_content_in_wrapper_prompt` asserts a representative upstream heading (`### 24. Excessive Hedging`) is absent from the wrapper and present only in the pinned file.

**Interactive-only behavior neutralized without modifying the upstream file:** confirmed. The wrapper explicitly instructs "No clarifying questions... do not invoke or reference AskUserQuestion" and "No voice sample... skip the Voice Calibration step," addressing the upstream frontmatter's `allowed-tools: [..., AskUserQuestion]` and its optional voice-sample branch, entirely from the separate wrapper file — the pinned `SKILL.md` itself is untouched (SHA-256-verified above).

**System-prompt size and per-call resend:** measured directly —

```
pinned SKILL.md:        33,935 characters
wrapper body:             3,091 characters (after header-line stripping)
composed system prompt: ~37,033 characters ≈ 9,258 tokens (chars/4 estimate)
```

**The entire ~9,258-token composed prompt is resent, unchanged, on every single beat call** — `_system_prompt()` caches the *string* at module scope (`hypogeum/agents/humanizer_llm_adapter.py`, `_SYSTEM_PROMPT` global) so the file is only read from disk once, but each `call_claude()` invocation still transmits the full text as that call's `system` message; OpenRouter/Anthropic bill system-prompt tokens on every call regardless of local caching, since there is no prompt-caching parameter set on the request. This is the dominant cost and latency driver identified in §4's table — the actual per-beat narration content is typically a few hundred characters, dwarfed by the repeated ~37KB instruction block.

---

## 6. Failure Behavior (Audit Q5)

Confirmed directly against `hypogeum/agents/humanization_stage.py::_build_shadow_beat()` and exercised by `tests/test_humanization_stage.py::TestShadowModeFallback`:

| Condition | Confirmed behavior |
|---|---|
| Provider failure (`OpenRouterError` subclass) | `call_humanizer_llm()` raises `HumanizerLLMAdapterError`; `_build_shadow_beat()` catches it, logs `hs_fallback_used`, reverts that beat to the deterministic-pass result. `llm_pass_applied=False` for that beat. |
| Malformed JSON / wrong shape | Same path — `_parse_humanized_text()` raises `HumanizerLLMAdapterError` inside the adapter; identical fallback. |
| Timeout | Surfaces from `call_claude()` as a `requests.Timeout` → retried once internally → on exhaustion, falls to Anthropic-direct or eventually raises an `OpenRouterError` subclass, which the adapter maps to `HumanizerLLMAdapterError` exactly as above. No special-case timeout handling exists or is needed beyond this. |
| Preservation failure (hard check fails on a successful, well-formed LLM response) | `_build_shadow_beat()` reverts that beat to the deterministic-pass result and sets `transformation_status="reverted"`, `fallback_used=True` — the **rejected** LLM text is never written into `humanized_narration` (verified by `test_reverted_beat_never_uses_unvalidated_llm_text`). |
| Partial-episode failure (some beats fail, others succeed) | Fully isolated per beat — confirmed by construction (each beat's try/except is independent) and by test coverage across date/number/quote/URL/name/uncertainty-loss scenarios, each exercised on a single-beat fixture. |
| Schema failure of the **assembled** package (a structural bug, not a content failure) | Not a per-beat fallback — `HumanizationStageError` propagates out of `run_humanization_stage()`, and (per the OFF-mode wiring task's exception-handling design, unchanged here) the entire pipeline halts and no checkpoint is written. This is the one failure mode that is *not* absorbed, by design — matching `docs/architecture/HUMANIZATION_STAGE_DESIGN.md` §11's explicit "structural problem, not content-quality problem" distinction. |

**Does a failed beat retry?** Only at the `call_claude()` layer's built-in `RETRY_MAX=1` (network/5xx/429 only). There is no stage-level retry of a beat after a `HumanizerLLMAdapterError` — a failure (of any kind, including a preservation-check rejection) is final for that beat within that run; the next opportunity to retry it is a full stage rerun (cache miss).

**Could repeated retries multiply cost?** No — established in §3: retryable HTTP failures occur before any billable generation.

**Is the original script the only downstream production input?** Confirmed. `TestHumanizationDownstreamIsolation::test_no_downstream_consumer_receives_humanized_script_package` uses a distinct sentinel object as the mocked Humanization Stage's return value and asserts it never appears in any argument passed to Finance, VPD, Audio, or Assembly's wrapper calls; a second test confirms `PipelineResult.script_package is not result.humanized_script_package` and that `script_package` is the identical object every downstream consumer received.

---

## 7. Checkpoint Behavior (Audit Q6)

**A valid SHADOW checkpoint prevents all repeat model calls on resume:** confirmed. `_run_humanization_stage()` in `pipeline.py` calls `load_reusable_humanization_checkpoint()` **before** calling `run_humanization_stage()` at all (`pipeline.py:1434-1493`) — on a hit, the function returns immediately; `run_humanization_stage()` (and therefore every `call_humanizer_llm()` call) is never invoked. Verified by `TestShadowCheckpoint::test_shadow_checkpoint_cache_miss_then_hit`.

**A partially completed episode cannot reuse an invalid partial checkpoint — because no partial checkpoint exists at all.** This is a genuine gap, not a false-positive-reuse risk: `save_humanization_checkpoint()` is called exactly once, only after **every** beat in the episode has been processed and the full package has passed schema validation (`humanization_stage.py`'s single `run_humanization_stage()` call, `pipeline.py:1489`). There is no per-beat or partial write. Consequently:
- A process crash or kill partway through (e.g. after beat 41 of 100) leaves **zero** checkpoint state — the file simply does not exist.
- On resume, `load_reusable_humanization_checkpoint()` correctly reports a clean miss (not a corrupt-checkpoint error — verified by `TestReuseBehavior::test_no_checkpoint_is_reuse_miss_not_error`).
- The stage then reruns **from beat 1**, re-calling (and re-billing) every beat that had already succeeded before the crash, in addition to the ones that hadn't. This directly answers Q6's "would per-beat checkpointing be needed if batching or long episodes are introduced?" — **yes**: the current all-or-nothing checkpoint design means the wasted-spend cost of a mid-run interruption scales with episode length, and will scale further (per-batch, not per-beat, granularity) if batching is introduced without a corresponding partial-persistence mechanism. Flagged as Finding D5.

**Model ID, wrapper version, skill SHA, mode, and source content changes invalidate reuse:** confirmed — no code change was needed for this, since `hypogeum/humanization_fingerprint.py::build_humanization_fingerprint()` already folded `mode` and `model_id` into its canonical payload before SHADOW mode existed. Verified directly (not just by source inspection) by `TestShadowCheckpoint::test_shadow_checkpoint_not_reused_for_off_mode_request` and `test_shadow_checkpoint_invalidated_by_model_id_change`, plus the pre-existing `TestReuseBehavior::test_changed_stage_version_produces_cache_miss` / `test_changed_wrapper_version_produces_cache_miss` / `test_changed_skill_sha_produces_cache_miss`.

---

## 8. Test-Strength Findings (Audit Q7)

**Tests that assert source-code strings instead of runtime behavior:** two pre-existing tests (both updated, not newly introduced, by the SHADOW-mode commit) read `pipeline.py`'s file content and `assert` substrings — `tests/test_humanization_stage.py::TestIsolation::test_referenced_by_pipeline_in_shadow_mode_only` and the equivalent test in `tests/test_humanization_checkpoint.py`. These prove the *wiring exists* (e.g. `_HUMANIZATION_MODE = "SHADOW"` literally appears in the file) but prove nothing about runtime call order, spend timing, or the Finance-Gate relationship described in §3 — a `pipeline.py` edit that kept the string present but silently reordered stages, or introduced a config override that changed the effective mode at runtime, would not be caught by either test.

**Confirmed test-coverage gap directly related to Finding D1:** no test in `tests/test_pipeline.py` asserts the call order between Humanization Stage (`"hs"`) and Finance Agent's budget authorization (`"fa"`). `TestAgentCallOrder::test_agents_called_in_frozen_order` tracks `["ep","rd","na","se","hs","vpd","td","audio","asm","pub"]` but does not instrument `"fa"` with an order-recording side effect at all — Finance runs for real (deterministically, no LLM) in that test, untracked. This means the SHADOW-mode implementation's own test suite never encoded the ordering fact that makes Finding D1 possible, and would not regress-detect a *worsening* of D1 (e.g. Humanization moved even later, after more spend-incurring stages) if one were introduced.

**Mocks that could conceal routing, accounting, or retry defects:** `tests/test_humanizer_llm_adapter.py::TestProviderFailure` and `tests/test_humanization_stage.py::TestShadowModeFallback` both mock at the boundary of `call_claude()` (adapter tests) or `call_humanizer_llm()` (stage tests) — meaning **no test in this feature's suite ever exercises `openrouter.py`'s actual retry loop, backoff timing, or the `_use_anthropic_for_run` fallback-flag interaction** described in §4. The adapter's *mapping* of a raised `OpenRouterError` to `HumanizerLLMAdapterError` is tested; the *conditions that produce* that exception (retry exhaustion, auth-triggered fallback, the process-global flag flip) are not exercised through this adapter by any test — consistent with the rest of the codebase's convention (e.g. `test_agent.py` mocks at the same boundary for `run_agent()`), so this is not a defect unique to Humanization, but it is a real limit on what the test suite proves about retry/fallback integration specifically for this new, much-higher-call-volume code path.

**Cost-tracking tests are mock-verified, not DB-verified:** `TestCostTracking::test_record_cost_called_on_success` asserts `record_cost` was *called* with the right keyword arguments; it does not exercise a real `set_run_context()` + SQLite write + `write_episode_rollup()` round trip. This matches the codebase-wide convention for agent-level cost tests and is not flagged as a defect, only as a scope limit.

**Confirmed: no test permits a direct external call.** Every test that reaches `call_humanizer_llm()` patches either `hypogeum.agents.humanizer_llm_adapter.call_claude` or `hypogeum.agents.humanization_stage.call_humanizer_llm`. `tests/test_humanizer_llm_adapter.py::TestIsolation` additionally asserts the adapter's own source contains no `OpenAI(`, `anthropic.Anthropic(`, `requests.`, `httpx.`, or `subprocess.` literal. Re-verified in this audit (§ Verification below) with a fresh, targeted grep across both new modules.

**Confirmed: tests do prove `humanized_script_package` never reaches Finance, VPD, Thumbnail, Audio, Assembly, or Publishing.** `tests/test_pipeline.py::TestHumanizationDownstreamIsolation` uses a distinct sentinel object (not equal-by-content to any other fixture) as the Humanization Stage's mocked return value and asserts it is absent from every argument passed to `"fa"`, `"vpd"`, `"audio"`, `"asm"`. Thumbnail Director is not separately checked here because — confirmed by design and by the existing pipeline wiring — TD never receives `script_package` or `humanized_script_package` directly at all; it consumes `visual_package`/the thumbnail handoff brief, which are themselves derived from `script_package` upstream of Humanization's insertion point. Publishing Agent similarly receives only `final_render_package`, several stages downstream, with no direct narration-package parameter — so a sentinel-object check at that boundary would be testing a parameter that doesn't exist rather than proving isolation. The isolation claim for TD/Publishing rests on the absence of any code path passing `script_package`/`humanized_script_package` to them at all (confirmed by reading `pipeline.py`'s full call-argument list for both), not on a dedicated sentinel test — this is a minor test-completeness gap worth noting but not a defect, since there is genuinely nothing to intercept.

---

## 9. Confirmed Defects (ranked by severity)

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| **D1** | SHADOW Humanizer spend occurs before, and is entirely excluded from, Finance Agent's Phase-1 budget-authorization gate and cost estimate. A blocked episode still pays for every already-run beat; an authorized episode's actual spend can silently exceed the reported estimate. | **MEDIUM-HIGH** (real, ungated, currently-small-but-nonzero spend; no data corruption or downstream leakage) | §3; `pipeline.py:611-640`; `finance_agent.py:85-105` has no Humanization term |
| **D5** | No per-beat/partial checkpoint — a mid-episode crash forces a full stage rerun and re-spend for every beat, including ones already successfully processed and billed. | **MEDIUM** (spend waste + latency on interruption; worsens with longer episodes or future batching) | §7; `pipeline.py:1489` single terminal `save_humanization_checkpoint()` call |
| **D2** | No test encodes the Humanization-before-Finance-authorization call order or the spend/gating relationship — the ordering that enables D1 is invisible to the test suite. | **LOW-MEDIUM** (test-coverage gap, not a runtime defect on its own) | §8; `TestAgentCallOrder` omits `"fa"` from order tracking |
| **D6** | `openrouter.py`'s `_use_anthropic_for_run` fallback flag is a never-reset, process-lifetime global; SHADOW mode's much higher per-episode call volume materially raises the odds of tripping it, silently rerouting *all* subsequent stages/episodes to Anthropic-direct. | **MEDIUM** (pre-existing design flaw, amplified — not introduced — by this feature) | §4; `openrouter.py:66-79`, confirmed never called from `pipeline.py` |
| **D3** | `humanizer_llm_adapter.py` duplicates `_strip_markdown_fence()` from `hypogeum/agent.py` instead of importing it. | **LOW** (verified byte-identical today; future drift risk only) | §2 |
| **D4** | Humanizer LLM calls emit a private `humanizer_llm_call_*` log-event namespace, disjoint from the `agent_run_*` family every other Claude call uses — cross-agent observability tooling built on the latter will not see these calls. | **LOW** (observability completeness, not correctness) | §2 |

---

## 10. Non-Defect Observations (confirmed correct — no action needed)

- **N1.** Direct `call_claude()` use is an architecturally defensible choice for a per-item, non-package call — see §2's verdict.
- **N2.** Retry and provider-fallback behavior is fully and correctly inherited from `openrouter.py`, identical to every other Claude call site in the repo.
- **N3.** Cost is recorded exactly once per successful call; retried/failed HTTP attempts are never billed, so no double-counting or retry-driven cost multiplication is possible.
- **N4.** The pinned `SKILL.md` is loaded byte-identical — SHA-256-verified independently in this audit, not merely asserted by a prior test.
- **N5.** The wrapper prompt does not duplicate or paraphrase upstream content.
- **N6.** Interactive-only upstream behavior (`AskUserQuestion`, the voice-sample branch) is correctly neutralized entirely from the separate wrapper file, with the pinned upstream file untouched.
- **N7.** Every per-beat failure mode (provider failure, malformed output, preservation-check rejection) correctly falls back to the deterministic-pass result and never allows unvalidated LLM text into `humanized_narration`.
- **N8.** SHADOW checkpoints are correctly distinguished from OFF checkpoints via the pre-existing fingerprint `mode`/`model_id` fields — no code change was required for this guarantee; it was designed in ahead of SHADOW's implementation.
- **N9.** `hypogeum/agents/provider_router.py` (read per the task's file list) is architecturally unrelated to this feature — it governs MUAPI video-provider tier routing (T1/T2/T3), not LLM text-provider routing. No coupling exists or is implied anywhere in the Humanization code path.
- **N10.** `humanized_script_package` is confirmed unreachable by Finance, VPD, Audio, and Assembly via sentinel-object tests; Thumbnail Director and Publishing Agent were confirmed unreachable by argument-list inspection (they never receive `script_package` or `humanized_script_package` at all, at any pipeline version).

---

## 11. Exact Smallest Next Implementation Step

**Not implemented by this audit.** The smallest, most narrowly-scoped safe fix for the highest-severity finding (D1) is:

> Reorder `run_pipeline()` so Finance Agent's Phase-1 budget-authorization gate (stage 6, `_run_budget_authorization`) runs **before** Humanization Stage (currently 5b), exactly as it already gates every later stage's prospective spend (VPD, Audio, Assembly). This is a pure stage-reordering change in `pipeline.py` — it requires no schema change, no checkpoint change, and no change to `humanization_stage.py` or `humanizer_llm_adapter.py` at all. It should ship as its own single commit with its own dedicated test (`TestAgentCallOrder`-style, explicitly asserting `"fa"` precedes `"hs"` in call order) closing the D2 gap in the same change, since the fix and its regression test are inseparable.

A secondary, independent follow-up (not required to close D1, but recommended once D1 is fixed) is to add a flat per-episode Humanizer cost term to `finance_agent.py`'s estimate function, mirroring `_OPENROUTER_PLANNING_FLAT`, so the authorization *estimate* — not just the *gate timing* — accounts for SHADOW-mode spend.

Neither change is made by this task, per its explicit read-only scope.

---

## 12. Final Decision

**MINIMAL REPAIR.**

The core SHADOW-mode mechanism — per-beat deterministic-then-LLM processing, preservation validation, per-beat fallback, whole-package schema validation, checkpoint fingerprinting, and downstream isolation — is sound, correctly implemented, and well-tested for what it tests. Nothing found here indicates the underlying design is wrong or that SHADOW mode should be paused or unwound. The one finding with genuine financial/safety consequence (D1: ungated, unaccounted spend ahead of the budget gate) is narrow, well-understood, and fixable with a single, self-contained stage-reordering change plus one new test — not a symptom of a deeper architectural problem. The remaining findings (D2–D6) are test-coverage, observability, and efficiency gaps appropriate to track and fix incrementally, not blockers to continued SHADOW-mode operation or to the next phase of Humanizer work.

**Recommendation:** land the D1/D2 fix (§11) as the next task before any consideration of `LLM_CANARY` or a downstream-consumer switch. Track D3, D4, D5, D6 as separate, lower-priority follow-ups; none of them block continued SHADOW-mode evaluation.
