# GPT Image 2 Thumbnail Routing — Audit

Read-only audit. No production code, `.env`, or runtime file was modified. No provider call was made. No image was generated.

Method: Graphify-first orientation, then direct reads of `hypogeum/muapi.py`, `hypogeum/config.py`, `hypogeum/agents/thumbnail_generation_agent.py`, `hypogeum/agents/thumbnail_compositor.py`, `.env`, `docs/architecture/GPT_IMAGE2_THUMBNAIL_SKILL_ATTACHMENT_AUDIT.md`, `docs/architecture/ARCHITECTURE_AUDIT.md`, `docs/architecture/UPSTREAM_SKILL_MANIFEST.md`, `docs/architecture/SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md`, `docs/dashboard-product-architecture.md`, `docs/model-output-comparison-framework.md`, `tests/test_muapi.py`, and `git log` for recent thumbnail-related commits.

---

## 1. The skill vs. the model — deconflicted first

`docs/architecture/GPT_IMAGE2_THUMBNAIL_SKILL_ATTACHMENT_AUDIT.md` (dated 2026-07-11) is about the **GPT-Image2 Claude Code skill** — a prompt-writing/technique reference attached to `thumbnail_prompt_writer.py`'s system prompt. That audit itself states plainly: *"Provider routing, image-generation models, and candidate counts are structurally unreachable from a prompt-writing skill attached at this point"* (N3) and *"GPT-Image2 is documentation for OpenAI's `gpt-image-2` model"* (N4) — i.e., it never claimed the skill controls which model generates the image. Git history (`b5270ea`, `f22b014`) confirms the skill was installed and attached after that audit, superseding its "not installed" finding on that one point only. **This audit is entirely separate**: it concerns the actual image-generation model, not the prompt-craft skill.

---

## 2. Two independent thumbnail-generation code paths exist

**Path A — single-image (`generate_thumbnail()`, `hypogeum/muapi.py:1645`)**, used by `thumbnail_generation_agent.py::generate_thumbnails()` for LEGACY_SCENE_MODE candidates (ep_001, ep_002 — no `composite_mode`):
- Provider priority chain: `config.THUMBNAIL_IMAGE_PROVIDER` → `THUMBNAIL_FALLBACK_PROVIDER` → `THUMBNAIL_FALLBACK2_PROVIDER`.
- `.env` values (confirmed, not code defaults alone): `THUMBNAIL_IMAGE_PROVIDER=openrouter`, `THUMBNAIL_FALLBACK_PROVIDER=openai`, `THUMBNAIL_FALLBACK2_PROVIDER=muapi`, `THUMBNAIL_FALLBACK_MODEL=flux-2-dev`.
- Concretely: **OpenRouter (`openai/gpt-5-image-mini`) is attempted first**; OpenAI direct (`gpt-image-1`) second; MUAPI last — and *within* the MUAPI branch, `gpt-image-2-text-to-image` (`config.THUMBNAIL_IMAGE_MODEL`) is attempted before its own internal fallback `flux-2-dev`.
- No resolution/quality/seed/negative-prompt parameters are sent to OpenRouter's chat-completions image-modality endpoint (`{"model", "messages", "modalities": ["image"]}` only) — size is enforced afterward by local `_normalize_thumbnail()` cropping, not requested from the provider.

**Path B — layered composite (`generate_and_composite()`, `hypogeum/agents/thumbnail_compositor.py:601`)**, used whenever `candidate["composite_mode"] is True` — which `_attach_concept_plans()` sets unconditionally for every concept-driven candidate (ep_003):
- Three separate MUAPI calls per candidate — `base_scene`, `hero_subject`, `fx` — each via `_generate_layer()` → `_generate_thumbnail_with_model(model=config.THUMBNAIL_IMAGE_MODEL, ...)`.
- `config.THUMBNAIL_IMAGE_MODEL` default is `gpt-image-2-text-to-image`, **used directly and unconditionally** — no OpenRouter, no OpenAI, no fallback chain at all.
- Typography is rendered separately by `_render_typography_pil()` — deterministic PIL, never sent to any image-generation model. Wordmark applied last by `_apply_wordmark()`.
- **This path is already canonically GPT Image 2** — confirmed, no drift found here.

---

## 3. Where "flux-dev-image" came from in the earlier trace

`generate_thumbnail()`'s own docstring (line ~1662) reads: *"Submits an image generation job to flux-dev-image, polls for completion..."* — a **stale docstring** describing an older, single-provider implementation that predates the current multi-provider fallback chain written beneath it. No current default configuration path actually selects `flux-dev-image`; it exists only as one entry in a general Flux-model recognition set (`hypogeum/muapi.py:120-128`), never assigned by any default or `.env` value. My earlier trace correctly found working, real code — but reported the docstring's stale framing instead of the actual provider-chain logic beneath it. This is now corrected in §2.

---

## 4. What "GPT Image 2" concretely is in this codebase

- **Model identifier**: `gpt-image-2-text-to-image` (text-to-image) / `gpt-image-2-image-to-image` (image-to-image) — members of `_GPT_IMAGE_2_MODELS` (`hypogeum/muapi.py:132`).
- **Provider**: MUAPI aggregator (`endpoint = f"{config.MUAPI_BASE_URL}/{model}"`, `hypogeum/muapi.py:2048`).
- **Real generation entry point**: `hypogeum/muapi.py::_generate_thumbnail_with_model()` (line 2027) — the one function that actually submits, polls, and downloads for this model family. Called from two places: inside `_attempt_thumbnail_generation()` (Path A's MUAPI branch) and directly from `_generate_layer()` (Path B).
- **Confirmed supported parameters** (from the actual payload, `hypogeum/muapi.py:2050-2061`): `prompt`, `aspect_ratio`, `resolution` (`config.THUMBNAIL_IMAGE_RESOLUTION`, default `"2K"`), `quality` (`config.THUMBNAIL_IMAGE_QUALITY`, default `"high"`).
- **Confirmed NOT supported** by this payload: seed, negative prompt, reference-image input, multiple-outputs-per-call (`n`). Each call returns exactly one image; N independent generations require N separate calls.
- **Documented cost**: $0.09/gen (`docs/architecture/ARCHITECTURE_AUDIT.md:403`, and matches `MODEL_CREDIT_COSTS`'s fallback default in `muapi.py`, which has no explicit dict entry for this model but resolves to the same figure).
- **Documented canonical status**: `docs/architecture/ARCHITECTURE_AUDIT.md` lists it as *the* thumbnail line item in its model/cost table; `docs/dashboard-product-architecture.md:619` shows it as the dashboard's "Image Model" dropdown value; `docs/architecture/UPSTREAM_SKILL_MANIFEST.md:192` states generation "still executes through the existing MUAPI `THUMBNAIL_IMAGE_MODEL` route." All three treat it as singular/canonical — none describe or justify the OpenRouter-first chain in Path A.
- **Pre-existing, unresolved, separately-tracked discrepancy** (found identically in `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md:124` and `UPSTREAM_SKILL_MANIFEST.md:193`): an external tool independently calls `model="gpt-image-2"` against the real OpenAI API, contradicting `hypogeum/config.py`'s comment that "gpt-image-2 is MUAPI-internal only." Both prior audits explicitly mark this "out of scope" and unresolved. This audit does not resolve it either — it does not block a routing decision, since MUAPI's own aggregator implements this model server-side regardless of whether "gpt-image-2" also happens to be a literal OpenAI SDK string.

---

## 5. Configuration mismatch

**Yes — one real mismatch, one documentation-drift artifact, one pre-existing tracked-separately item (not this audit's to resolve).**

1. **Real mismatch**: Path A's default provider order (`openrouter → openai → muapi`) does not match the "GPT Image 2 is canonical" framing consistently documented in `ARCHITECTURE_AUDIT.md`, `dashboard-product-architecture.md`, and `UPSTREAM_SKILL_MANIFEST.md`. No document found justifies deprioritizing MUAPI/GPT-Image-2 below OpenRouter's `gpt-5-image-mini` for Path A specifically.
2. **Documentation drift**: `generate_thumbnail()`'s docstring is stale (§3) — not a behavioral bug, but a real misleading-trace risk, as this investigation demonstrated firsthand.
3. **Pre-existing open item, not new**: the "is gpt-image-2 a real OpenAI model" discrepancy (§4) — already flagged twice before this audit; carried forward unresolved, exactly as the prior audits left it.

**Root cause**: Path A (single-image) and Path B (layered composite) were built in separate commits with separate provider-selection philosophies (`git log`: multi-layer compositor work — `017b1ce`, `99bf26a`, `da93f1f` — is a distinct line of work from the provider-fallback chain in Path A). Path B was wired directly to `config.THUMBNAIL_IMAGE_MODEL` from the start and never needed reconciling. Path A's fallback chain was never revisited against the architecture docs' "GPT Image 2 canonical" framing once that framing was written.

---

## 6. Proposed canonical routing design (design only — not implemented)

- **Single source of truth**: `config.THUMBNAIL_IMAGE_MODEL` (`gpt-image-2-text-to-image`) should be Path A's **primary** attempt too, not its last resort — bringing Path A into alignment with Path B and with the documented architecture, rather than inventing a third convention.
- **Concretely**: reorder Path A's default provider chain so `muapi` (GPT Image 2) is primary, with `openrouter` and `openai` retained as fallbacks (roles reversed from today) — *or*, if OpenRouter-first was in fact an intentional, documented cost/quality choice for legacy-mode candidates specifically, that intent should be written down explicitly (it currently is not, anywhere this audit found) rather than left implicit. This is a decision for the team to make explicitly, not one this audit makes unilaterally.
- **Path B (layered composite) requires no change** — already correctly uses GPT Image 2 directly for all three layers, with real resolution/quality control. Recommend explicitly preserving this as-is.
- **Typography should remain deterministic PIL, not model-generated**, for both paths. Production already made this call for Path B (`_render_typography_pil`, "never via MUAPI"); nothing in this audit found a reason to reverse it — GPT-Image-2's advertised exact-text capability (from the *skill* documentation, not the generation payload) has not been validated against Hypogeum's own zone/wordmark/brand requirements, and reversing a working, deterministic, brand-guaranteed step to a probabilistic one is a materially different, larger decision than a routing-priority fix.
- **Fix the stale docstring** in `generate_thumbnail()` regardless of the routing decision — it actively misleads anyone tracing this code path, as demonstrated.

---

## 7. Minimum files requiring change (identification only)

- `.env` and/or `hypogeum/config.py` (`THUMBNAIL_IMAGE_PROVIDER` default) — only if the team decides to reorder Path A's chain.
- `hypogeum/muapi.py` — `generate_thumbnail()` docstring correction (independent of the routing decision).
- `hypogeum/agents/thumbnail_generation_agent.py` — verify no code assumes today's specific ordering before any reorder.
- No change needed to `thumbnail_compositor.py` / Path B.

## 8. Tests required (identification only)

- An explicit test asserting `config.THUMBNAIL_IMAGE_MODEL` default (`tests/test_muapi.py` already assigns this value in fixtures at line ~2493 but was not confirmed to assert it as *the real default* — needs a dedicated default-value test).
- A provider-priority-order test for Path A reflecting whichever ordering the team decides on, with an explanatory comment (currently no such test was found confirming today's `openrouter → openai → muapi` order is intentional).
- A regression test protecting Path B's existing, already-correct behavior (`generate_and_composite()` always calls `_generate_thumbnail_with_model` with `config.THUMBNAIL_IMAGE_MODEL` for all three layers) — guards against an unintended regression while Path A is being changed.

---

## Answers

1. **Approved canonical provider**: MUAPI aggregator.
2. **Approved canonical model identifier**: `gpt-image-2-text-to-image` (`config.THUMBNAIL_IMAGE_MODEL`).
3. **Real generation entry point**: `hypogeum/muapi.py::_generate_thumbnail_with_model()` (line 2027), reached via `_attempt_thumbnail_generation()` in Path A or directly via `_generate_layer()` in Path B.
4. **Current runtime model actually selected**: mode-dependent — Path A (ep_001/ep_002, legacy mode) defaults to `openai/gpt-5-image-mini` via OpenRouter (GPT Image 2 only reached as last-resort fallback); Path B (ep_003, concept-driven/composite mode) already uses `gpt-image-2-text-to-image` directly and exclusively.
5. **Configuration drift found**: yes.
6. **Root cause of drift**: two thumbnail-generation code paths (single-image vs. layered composite) built independently, with Path A's fallback chain never reconciled against the "GPT Image 2 canonical" framing documented elsewhere.
7. **Canonical target routing**: MUAPI/`gpt-image-2-text-to-image` as primary for both paths — already true for Path B; requires an explicit provider-order decision (and possible reorder) for Path A. Typography stays deterministic PIL for both.
8. **Minimum files requiring change**: `.env`/`config.py` (`THUMBNAIL_IMAGE_PROVIDER`, if reordering Path A), `hypogeum/muapi.py` (stale docstring), `hypogeum/agents/thumbnail_generation_agent.py` (verification only).
9. **Whether ep_003 layered generation can use GPT Image 2**: yes, for all three layers (`base_scene`, `hero_subject`, `fx`) — it already does. Typography should remain deterministic/PIL, not generated.
10. **Required tests**: `THUMBNAIL_IMAGE_MODEL` default-value test, Path A provider-order test (post-decision), Path B regression-protection test.
11. **Files modified**: none.
12. **Paid calls made**: 0.
13. **Output path**: `docs/validation/thumbnail_image_generation_validation/GPT_IMAGE_2_ROUTING_AUDIT.md`.
