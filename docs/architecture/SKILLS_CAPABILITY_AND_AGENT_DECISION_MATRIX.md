# Skills Capability and Agent Decision Matrix

**Date:** 2026-07-10 (corrected)
**Status:** CANONICAL — this document is the source of truth for the next implementation phase. No installation or wiring may proceed until this document is explicitly approved.
**Method:** Graphify-first orientation, then targeted reads of required governance documents (`PROJECT_CONSTITUTION.md`, `PROJECT_PRINCIPLES.md`, `CURRENT_PHASE.md`, `DECISION_LOG.md`, `docs/architecture/SKILLS_AND_AGENTS_AUDIT.md`, `docs/architecture/SKILL_PROVENANCE_AND_AGENT_MAPPING.md`, `docs/architecture/GRAPHIFY_FIRST_WORKFLOW.md`, `docs/product-experience-system.md`, `docs/product-experience-system-governance.md`, `docs/channel-profile-system.md`), plus a full-repository search (`docs/`, `hypogeum/`, `tests/`, case-insensitive) for every system/agent name required by this correction pass.

---

## Correction Provenance Note — read this before the rest of the document

This is a correction of the version committed at `1361ceb`. The correction instructions required this document to describe an existing/approved architecture including named systems: **UX/UI Director, Brand & Identity Director, Channel Creation Wizard, Brand Compiler, Content Repurposing Director, Carousel Content Director, Carousel Visual Designer, Carousel Image Prompt Writer, Carousel Compositor, Master Episode Package, Episode Repurposing Brief.**

**A full-repository search (`grep -ri` across `docs/`, `hypogeum/`, `tests/`, plus `graphify query`) found none of these names anywhere in this repository — not in `docs/product-experience-system.md`, not in `docs/product-experience-system-governance.md` (the two documents specifically named as sources for this section), not in `docs/channel-profile-system.md`, not in code.** What those two documents actually define, and what real code exists, is materially different from the requested names:

| Requested name | What actually exists |
|---|---|
| "UX/UI Director" | **"UX/UI Designer"** — an engineering *role* (B4.95), not a distinct Director-tier agent (`docs/product-experience-system-governance.md:6`) |
| "Channel Creation Wizard" (as a named agent/system) | A generic, lowercase **"channel creation wizard"** — a multi-step UI flow component (`docs/dashboard-product-architecture.md:165,1312,1315`; `docs/factory-template-system.md:95,635,782`), not an AI agent |
| "Brand & Identity Director" | Does not exist under any name. The closest real, already-implemented mechanism is `hypogeum/factory_clone.py` — deterministic functions (`duplicate_channel()`, `clone_factory()`, `export_brand_package()`/`import_brand_package()`, `export_identity_package()`/`import_identity_package()`, `ChannelTemplate`) that deep-clone a channel's brand/identity configuration. This performs the *cloning* half of the requested responsibility today, but does no AI-driven "differentiated visual identity generation" — that half does not exist in any form. |
| "Brand Compiler" | Does not exist under any name. No compilation step from raw brand config into a distributable "Channel Brand Package" is implemented. |
| "Content Repurposing Director," "Carousel Content Director," "Carousel Visual Designer," "Carousel Image Prompt Writer," "Carousel Compositor," "Master Episode Package," "Episode Repurposing Brief" | None exist under any name. The only adjacent, already-drafted (not implemented, not approved — `docs/shorts_extraction_architecture.md:3`, "awaiting approval") design is Shorts extraction, which adds fields to existing Visual Package Director/Script Engine output and performs cropping via deterministic FFmpeg in `assembly_agent.py` — it does not touch carousels, static social images, or any cross-platform repurposing pipeline. |

Per `PROJECT_PRINCIPLES.md` §10 ("Never Trust Assumptions... Never guess") and the standing rule already established earlier in this same Skills Standardization Phase ("Claude must never recreate, paraphrase, or imitate an upstream skill as a replacement" — the same discipline applies to inventing internal architecture), **this document does not assert that these systems are existing or previously-approved architecture.** Sections 5 and 6 below record the requested chains, contracts, and ownership assignments in full, exactly as specified in the correction instructions — but labeled explicitly as **PROPOSED / NOT YET APPROVED**, filed under Roadmap Buckets C and D (§9), consistent with the correction instructions' own framing of those buckets as *future* work. This is not a refusal to comply — every requested chain, contract field, and agent responsibility is recorded below in full. It is a provenance correction: these are new design proposals authored in this pass, not a restoration of architecture this document previously omitted. If a document elsewhere in the repository does define these systems and this search missed it, that location should be supplied so this note can itself be corrected.

---

## Terminology Reconciliation (unchanged from the prior version — still applies)

The word **"skill" has three distinct, unrelated meanings in this repository**:

1. **B5.15a capability-permission skill** (`docs/agent-skills-capability-matrix.md`) — a fixed, 20-entry taxonomy of tool-use authorities (`web_search`, `image_generation`, `video_generation`, `model_invoke`, etc.), a governance/access-control concept enforced by the Agent Director at run start.
2. **B6 versioned behavior unit** (`docs/agent-operating-system.md` §7) — design-only, not implemented, Module-owned.
3. **Claude Code Skill** (`SKILL.md` files) — a markdown reference document loaded into an LLM's context via `compose_system_prompt()` or the Claude Code runtime. **This is the subject of this document.**

Adopting a Claude Code Skill into an agent does not require a B5.15a Rule 6 capability-grant approval — it enriches the content of an already-approved `model_invoke` call, it does not grant a new tool-use authority. This document governs meaning 3 only.

---

## 1. Phase Boundary

- **Source verification is complete** for all six reviewed sources (§2).
- **No external skill is installed yet, except Graphify** — the repository-navigation layer, out of scope for these decisions.
- **This phase decides capabilities, ownership, order, and integration boundaries only.** It does not touch any agent, prompt, provider client, or dashboard file.
- **Installation and wiring happen only after this document is approved**, as a separate future action.
- **Upstream skills must remain verbatim.** Hypogeum-specific behavior lives in thin wrappers/adapters, never inside the upstream file.
- **Claude must never recreate, paraphrase, or imitate an upstream skill as a replacement**, and — per the correction in this pass — must never assert invented internal architecture as pre-existing/approved. §5 and §6 are explicitly proposals, not installed-skill decisions, and require their own separate approval before anything in them is implemented.

---

## 2. Final Upstream Decision Matrix

### 2.1 Seedance2

| Field | Value |
|---|---|
| Canonical source | `https://github.com/dexhunter/seedance2-skill` |
| Artifact type | Claude Skill (pure markdown, bilingual EN/ZH) |
| License | MIT |
| Executable components | None |
| Useful capabilities | `@Reference` role-assignment syntax, input-limit tables — content already partially present in `hypogeum/skills/seedance_translation_skill.md` under evidence tag `SEED-COMM-01` |
| Incompatible components | Targets Jimeng Seedance 2.0 (480p–720p cap); Hypogeum's BytePlus Ark/MUAPI path documents a broader range (480p–4K on the full tier) |
| **Final decision** | **RECONCILE AND ADOPT FOR PROVIDER-PROMPT CAPABILITIES** |
| Exact target system/agent | `hypogeum/agents/visual_package_director.py` — **Stage B only** |
| Agents that must NOT receive it | `executive_producer.py`, `research_director.py`, `narrative_architect.py`, `script_engine.py`, `shot_diversity_planner.py`, `thumbnail_prompt_writer.py`, `thumbnail_identity_guardian.py`, `brand_guardian_agent.py`, `thumbnail_strategy.py`, `topic_ideator.py` |
| Wrapper required | Yes — see §3, three-layer separation |
| Installation priority | 2 |
| Current status | **VERIFIED / RECONCILIATION CANDIDATE / NOT INSTALLED** |

### 2.2 Veo Prompt Director

| Field | Value |
|---|---|
| Canonical source | `/Users/gawadeditor/Desktop/veo-prompt-director-1.0.0.tar.gz` |
| Artifact type | Skill package (mcpmarket-distributed), pure markdown |
| License | None found |
| Executable components | None |
| Useful capabilities | Emotion-to-observable-action table (confirmed absent from Hypogeum's existing skill), audio-direction syntax, multi-shot timestamp structure, compatible camera/composition vocabulary |
| Incompatible components | `references/cinematography.md` lists `Static` as a recommended movement |
| **Final decision** | **RECONCILE SELECTED CAPABILITIES; DO NOT INSTALL AS AN UNFILTERED RUNTIME PROMPT** |
| Exact target system/agent | `hypogeum/agents/visual_package_director.py` — **Stage B only** |
| Agents that must NOT receive it | Same list as §2.1 |
| Wrapper required | Yes — see §3 |
| Installation priority | 3 |
| Current status | **VERIFIED PACKAGE / SELECTIVE CAPABILITY RECONCILIATION / NOT INSTALLED** |

**Conflict-handling clarification:** the `Static`-camera recommendation is not itself a reason to reject this source. Verified directly (`hypogeum/brand/video_style_system.md:345`): Hypogeum's actual rule forbids "static hold as the primary camera movement for **non-establishing** scenes"; a controlled hold up to 8 seconds is explicitly permitted for establishing shots (`:339`, `:352-354`). The precedence policy in §3 encodes this precise scope, not a flattened "no static ever."

### 2.3 Veo Tools

| Field | Value |
|---|---|
| Canonical source | `https://github.com/kdowswell/veo-tools` |
| Artifact type | Claude Code Plugin with real executable automation |
| License | MIT |
| Executable components | `veo-generate.ts`, `veo-multi-generate.ts`, `assemble-clips.sh`, `create-loop.sh` — all call **Google Vertex AI directly** |
| Useful capabilities | 5-Element Formula (redundant with existing content); human-approval-gated workflow (not applicable to Hypogeum's unattended pipeline) |
| Incompatible components | The entire executable layer |
| **Final decision** | **DO NOT USE AS A CURRENT PIPELINE RUNTIME PROVIDER PATH** |
| Exact target system/agent | None. Not on the skill-installation priority list. |
| Agents that must NOT receive it | All pipeline agents — no current production agent receives it |
| Wrapper required | N/A under current scope. Retained only as an **engineering reference** for a future, separately approved GCP-direct provider path. Any such future path must first integrate with, in full: **Identity and Secret Management, Finance Gate, Provider Router, cost tracking, retry and failure policy, observability, autonomous runtime, provider comparison, and configuration/deployment portability** — matching the same integration surface every existing MUAPI-routed provider already satisfies. |
| Installation priority | Not ranked — reference-only, outside the active priority list |
| Current status | **VERIFIED REPO / CURRENT RUNTIME INCOMPATIBLE / NOT INSTALLED** |

This does not mean the repo is bad — its direct-execution scripts must not be attached to any current production agent because they would create a parallel generation route outside `Finance Gate → Provider Router → MUAPI/provider adapter → Cost Tracking`.

### 2.4 GPT-Image2 Skill

| Field | Value |
|---|---|
| Canonical source | `https://github.com/wuyoscar/GPT-Image2-Skill` |
| Artifact type | Hybrid — 34-file prompt/reference gallery + agentic skill + standalone Python CLI |
| License | MIT |
| Executable components | `src/gpt_image_cli/cli.py` — direct `openai` SDK calls, `OPENAI_API_KEY`, bills the user's OpenAI account directly |
| Useful capabilities | 33 curated `references/gallery-*.md` category files — a genuine capability gap Hypogeum has no equivalent of today |
| Incompatible components | The CLI's direct-OpenAI-SDK execution path — same bypass category as Veo Tools, against OpenAI instead of Vertex AI |
| **Final decision** | **SPLIT DECISION** — Prompt/reference capabilities: **PARTIAL ADOPTION CANDIDATE**. Direct CLI/OpenAI SDK runtime: **NOT ADOPTED UNDER CURRENT PROVIDER ARCHITECTURE.** This source is not merely reference-only overall — the reference-gallery half is a real adoption candidate; only the executable half is rejected. |
| Exact target system/agent (prompt/reference half only) | `hypogeum/agents/thumbnail_prompt_writer.py` (real, existing); Carousel Image Prompt Writer (**proposed**, §6 — not yet approved); a future Static Social Image Prompt Writer (**proposed**, §6 — not yet approved, no such agent exists today); Brand & Identity Director for visual-exploration briefs only (**proposed**, §5 — not yet approved, no such agent exists today) |
| Agents that must NOT receive it | All non-thumbnail/non-image agents; the CLI/script components must reach no agent at all, existing or proposed |
| Wrapper required | Yes, for the prompt/reference half if adopted. N/A for the CLI half, which is not adopted. |
| Installation priority | 4 |
| Current status | **VERIFIED REPO / PARTIAL ADOPTION CANDIDATE / NOT INSTALLED** |

**Open finding, tracked separately from adoption status:** this repo's CLI successfully calls `model="gpt-image-2"` against the real OpenAI API. Hypogeum's own `hypogeum/config.py:347` states "gpt-image-2 is MUAPI-internal only" — a claim this widely-used, actively-maintained external tool directly contradicts. This is a documentation-accuracy question for Hypogeum's own config comments, independent of this skill's adoption.

### 2.5 Humanizer

| Field | Value |
|---|---|
| Canonical source | `https://github.com/blader/humanizer` |
| Artifact type | Portable Claude/agent Skill (pure markdown), also a Claude Code Plugin |
| License | MIT |
| Executable components | None |
| Useful capabilities | 33 numbered AI-writing-tell patterns, voice calibration, draft→audit→final loop |
| Incompatible components | None found |
| **Final decision** | **STRONG ADOPTION CANDIDATE — RECONCILE INTEGRATION SHAPE, THEN ADOPT VERBATIM.** Ownership investigated against live code — see §4. |
| Exact target system/agent | A new, minimal **Humanization capability** placed after `hypogeum/agents/script_engine.py` (see §4) |
| Agents that must NOT receive it | `executive_producer.py`, `research_director.py`, `narrative_architect.py`, `visual_package_director.py`, `shot_diversity_planner.py`, `thumbnail_prompt_writer.py`, `thumbnail_identity_guardian.py`, `brand_guardian_agent.py`, `thumbnail_strategy.py`, `topic_ideator.py` |
| Wrapper required | Yes — see §4 for the exact constraint list |
| Installation priority | 1 |
| Current status | **VERIFIED / STRONG ADOPTION CANDIDATE / NOT INSTALLED** |

### 2.6 UI/UX Pro Max

| Field | Value |
|---|---|
| Canonical source | `https://github.com/nextlevelbuilder/ui-ux-pro-max-skill` |
| Artifact type | Large multi-skill toolkit (7 sub-skills), Claude Code Plugin with an npm CLI installer |
| License | MIT |
| Executable components | Multiple `.cjs`/`.py` scripts per sub-skill, including a Pexels web-scrape (no API key) and a design-token/slide generation engine |
| Useful capabilities | Design-system/component/typography/color-palette reference content — potentially relevant to §5's proposed brand tooling and to real, existing dashboard UI work |
| Incompatible components | None of the toolkit's *content* directly conflicts with an existing Hypogeum rule; the *integration point* question is the open item |
| **Final decision** | **PARTIAL MULTI-SYSTEM ADOPTION CANDIDATE / NOT INSTALLED.** Corrected from the prior version's "not adopted" — that was too narrow, because it evaluated only against currently-implemented pipeline agents and did not account for proposed future consumers. It is not limited to passive reference use. |
| Exact target system/agent | **Real, existing today:** the "UX/UI Designer" role (B4.95, `docs/product-experience-system-governance.md:6`) for dashboard frontend work — a human-operator-facing Claude Code session context, not a pipeline agent. **Proposed, not yet approved (§5):** Brand & Identity Director, Channel Creation Wizard (as an agent, distinct from the existing UI-flow component of the same lowercase name), Brand Compiler, Carousel Visual Designer. |
| Agents that must NOT receive it | `brand_guardian_agent.py` **must not receive the full toolkit** — it receives only compiled, channel-specific brand contracts (the proposed Channel Brand Package, §5), never the raw multi-skill toolkit. All non-UI, non-brand pipeline agents (`executive_producer.py`, `research_director.py`, `narrative_architect.py`, `script_engine.py`, `thumbnail_identity_guardian.py`, `topic_ideator.py`) must not receive it in any form. |
| Wrapper required | Yes, for any proposed target in §5 — the toolkit's raw content is never injected directly; a channel-specific compiled contract sits between it and any production consumer |
| Installation priority | 6 (lowest active-decision item; the real B4.95 dashboard-work use is available today without any wiring, since it's a human-operator Claude Code session tool, not a pipeline integration) |
| Current status | **VERIFIED REPO / PARTIAL MULTI-SYSTEM ADOPTION CANDIDATE / NOT INSTALLED** |

**Provenance caution (unchanged from the original verification):** ~104K stars via an organization account created 7 months prior to verification, heavy cross-tool topic-tag stuffing, a dedicated marketing site — atypical relative to the other five sources, worth independent trust-verification before relying on it as a reference, separate from the architectural-fit finding above.

---

## 3. Deterministic Precedence Policy (Visual Package Director Stage B)

Three separated layers govern what ultimately reaches VPD Stage B's composed system prompt:

1. **Provider-neutral visual intent** — the scene's narrative function, subject, action, and required coverage, as already produced by Script Engine / Scene Planner / Shot Diversity Planner, independent of which provider will render it.
2. **Channel/Hypogeum cinematic policy** — `hypogeum/brand/video_style_system.md`, `visual_design_principles.md`, `thumbnail_style_system.md`, and `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md`. Always wins over provider vocabulary.
3. **Selected-provider translation** — `veo_translation_skill.compact.md` / `seedance_translation_skill.compact.md` (or their reconciled successors per §2.1/§2.2), loaded **conditionally on which provider `provider_router.py` has actually assigned to the scene** — not both unconditionally, as today's composition does. A Veo-routed scene does not need Seedance vocabulary in its prompt and vice versa; conditional loading is itself a token-budget and cross-contamination improvement over the current always-both composition.

**Conflict precedence, highest to lowest:**

1. **Verified factual/physical constraints** — what a provider's API genuinely supports (duration ranges, resolution ceilings, parameter rejections) — these are not stylistic preferences and cannot be overridden by anything below.
2. **Channel brand and production policy** — `video_style_system.md` §11's camera narrative-function table, one-movement-per-scene rule, the explicit forbidden behaviors, hold-duration limits; `visual_design_principles.md`'s 20 principles.
3. **Provider contract/API constraints** — vocabulary and structural conventions the provider's own documentation recommends for reliable output (e.g. the `@Reference` system, `SFX:`/`Ambient noise:` prefixes).
4. **Optional upstream creative vocabulary** — anything in a reconciled upstream skill that tier 1–3 neither requires nor forbids; may supplement, never override.

**Concrete application to the `Static`-camera case:** tier 2 forbids static as the primary movement for non-establishing scenes specifically (not universally, per §2.2's clarification). The adapter must either omit the upstream `Static` entry from what it extracts, or retain it annotated with tier 2's exact scope restriction — never present it unqualified.

Upstream skills are never edited to enforce this. A thin adapter selects or references compatible sections and adds Hypogeum's own constraints as a separate layer, per §1's verbatim-preservation rule.

---

## 4. Humanizer Ownership Investigation

**Method:** direct inspection of live code — `hypogeum/pipeline.py`'s stage order, every module importing `script_engine`'s output, and the actual behavior of every QA-named agent in the codebase.

**Findings:**
- Script Engine's output (`script_package`) is consumed by `assembly_agent.py`, `audio_agent.py`, `finance_agent.py`, `scene_planner.py`, `thumbnail_strategy.py`, `visual_package_director.py` — none of these revise the narration prose itself; each consumes it for its own downstream purpose (timing, synthesis, cost, scene planning, thumbnail strategy, visual planning).
- `hypogeum/agents/editorial_qa.py` is explicitly **deterministic** — its own docstring states "No Claude calls — this is a deterministic Python evaluation," and its 8 checks (repeated presentation method, repeated camera movement, repeated environment, split-screen misuse, weak reveal, missing underground method, motif redundancy, pacing) are all **visual pattern checks across the Visual Package**, not narration prose review.
- `hypogeum/agents/qa_agent.py` and `hypogeum/agents/final_output_qa.py` are also deterministic (rule-based checks; `final_output_qa.py` specifically does FFmpeg silence/freeze detection on the rendered video).
- **No existing agent — Critic, Revision, or otherwise — reviews or improves narration prose between Script Engine and Audio Agent.** This confirms the correction instructions' own decision rule applies: "Propose a separate Humanization Agent only if live architecture proves no existing agent can own it without violating separation of concerns" — live architecture proves exactly that.

**Placement:** `narrative_architect.py` → `script_engine.py` → **[NEW: Humanization capability, thin wrapper around Humanizer skill]** → Final Script QA (does not exist today — this is also a gap, noted but out of scope to design here since it wasn't in the six verified sources) → `audio_agent.py` / `voice_generation_operator.py`.

**Constraints (verbatim from the correction instructions, retained as hard requirements for the wrapper):** the Humanization capability must operate only on narration fields and preserve verified facts, names, dates, quotes, evidence levels, uncertainty language, narrative intent, and schema structure. Enforcement mechanism: the existing `hypogeum/schemas.py` validation gate that already runs after every agent stage provides a natural backstop — the humanized output must still validate against `script_package`'s existing schema, and a dedicated fact-preservation check (comparing named entities/dates/quotes pre- and post-humanization) should be part of the wrapper's own validation, not left to schema shape alone. Not authorized for implementation by this document.

---

## 5. Proposed Channel Brand Architecture — Roadmap Bucket C (PROPOSED / NOT YET APPROVED)

**This entire section is a new design proposal.** No file in this repository defines it today (see Correction Provenance Note). It is recorded here in full per the correction instructions, filed under Roadmap Bucket C (§9), and requires its own separate approval before any part of it is implemented.

**Proposed chain:**

```
Channel Creation Wizard (proposed agent — distinct from the existing UI-flow
  component of the same lowercase name in docs/dashboard-product-architecture.md)
        │
        ▼
Brand & Identity Director (proposed agent)
  ├─ creating a new channel from scratch, OR
  └─ cloning an existing channel's operational configuration while
     generating a sufficiently differentiated visual identity
        │
        ▼
Approval / policy gate (proposed — would reuse the existing Approval System /
  B5.13 Authorization pattern already governing other HIGH-risk changes,
  per docs/agent-skills-capability-matrix.md Rule 6)
        │
        ▼
Brand Compiler (proposed agent — compiles raw brand decisions into a
  distributable, versioned package)
        │
        ▼
Channel Brand Package (proposed contract, output of Brand Compiler)
        │
        ▼
Production consumers (existing agents: visual_package_director.py,
  thumbnail_prompt_writer.py, thumbnail_director.py — receive only the
  compiled package, never raw brand-toolkit content)
        │
        ▼
Brand Guardian compliance (existing: brand_guardian_agent.py — validates
  production output against the compiled Channel Brand Package; receives
  the compiled package only, never the full UI/UX Pro Max toolkit)
```

**Grounding against real code:** the "cloning" half of Brand & Identity Director's responsibility already has a real, deterministic implementation to build on — `hypogeum/factory_clone.py`'s `duplicate_channel()` (deep-clones a channel profile into a new channel, "created so the clone is fully independent of the source," per its own docstring) and `export_brand_package()`/`import_brand_package()`/`export_identity_package()`/`import_identity_package()`. What does not exist is the AI-driven "sufficiently differentiated visual identity" generation step — `duplicate_channel()` performs an exact deep-copy, not a differentiated variant. Any Brand & Identity Director implementation would sit on top of this existing deterministic layer, not replace it.

**Channel Brand Package — required contract fields, per channel, each independently owned (not inherited by reference from another channel):**

- Editorial identity
- Audience profile
- Brand tokens
- Typography
- Color system
- Logo rules
- Thumbnail identity
- Video visual identity
- Carousel identity
- Voice/language rules
- Platform templates
- Forbidden patterns
- Compliance thresholds

**UI/UX Pro Max's role in this proposal:** its design-system/typography/color-palette/component reference content (§2.6) is a plausible input source for Brand Compiler's raw material and for Brand & Identity Director's visual-exploration briefs — never injected directly into `brand_guardian_agent.py` or any production consumer, only into the proposed authoring-time agents, and only after compilation into the channel-specific package above.

---

## 6. Proposed Carousel / Content-Repurposing Architecture — Roadmap Bucket D (PROPOSED / NOT YET APPROVED)

**This entire section is a new design proposal.** No file in this repository defines it today (see Correction Provenance Note). The closest existing adjacent design is `docs/shorts_extraction_architecture.md`, itself unapproved ("awaiting approval," `:3`) and narrower in scope (video-only, no carousels, no cross-platform repurposing). This section is filed under Roadmap Bucket D (§9) and requires its own separate approval.

**Proposed chain:**

```
Master Episode Package (proposed contract — the completed episode's full
  production output: script, research, visual package, thumbnails, final
  render, publishing metadata)
        │
        ▼
Content Repurposing Director (proposed agent)
        │
        ▼
Episode Repurposing Brief (proposed contract, verified — see fields below)
        │
        ▼
Carousel Content Director (proposed agent)
        │
        ▼
Carousel Visual Designer (proposed agent)
        │
        ▼
Carousel Image Prompt Writer (proposed agent)
        │
        ▼
Approved image-provider route (existing infrastructure: MUAPI, gated by
  Finance Gate/cost tracking — same route every current image-generation
  call already uses; no new provider path)
        │
        ▼
Carousel Compositor (proposed — deterministic, matching the pattern of
  hypogeum/agents/thumbnail_compositor.py: assembles generated images with
  text/branding, no LLM call)
        │
        ▼
Brand Guardian (existing: brand_guardian_agent.py — receives the compiled
  Channel Brand Package per §5, not raw content)
        │
        ▼
Fact QA (proposed — verifies carousel copy against the Episode Repurposing
  Brief's verified-facts field, preventing re-research or fact drift)
        │
        ▼
Platform QA (proposed — validates format/size/text-safe-zone compliance
  per target platform, analogous to existing thumbnail Shorts text-safe-zone
  checks already documented in .claude/skills/visual-review.md)
        │
        ▼
Publisher (existing: hypogeum/agents/publishing_agent.py — confirmed
  deterministic, zero model_invoke per docs/agent-skills-capability-matrix.md:751-767;
  uploads a pre-built, already-approved package exactly as it does today)
```

**Episode Repurposing Brief — required contract fields, exactly as specified:**

- Episode summary
- Verified facts
- Evidence restrictions
- Narrative angle
- Target audience
- Platform objective
- CTA
- Prohibited claims
- Channel brand package reference (§5's Channel Brand Package)
- Reusable approved assets

**Hard rule (verbatim):** social outputs must not research the episode again from scratch. The Episode Repurposing Brief is the single source of truth for every downstream repurposing agent — Content Repurposing Director performs the one verification pass against the Master Episode Package; every agent after it in the chain consumes the Brief only, never re-derives facts independently. This mirrors the existing Research Director → downstream-agent pattern already in place for the primary pipeline (only Research Director calls Perplexity; nothing downstream re-researches).

**Input/output contract at every transition:** each arrow in the chain above is a schema-validated handoff, matching the existing pattern established by every current pipeline stage (`hypogeum/schemas.py`'s `validate()` gate after every agent). No transition in this proposal is exempt from that pattern — a schema for each of the contracts named above (Master Episode Package, Episode Repurposing Brief, and each intermediate carousel artifact) would need to be authored in `hypogeum/schemas.py` before implementation, following the same structure as the 26 schemas that already govern the primary pipeline.

**GPT-Image2's role in this proposal:** its reference-gallery content (§2.4) is the plausible skill source for Carousel Image Prompt Writer and any future Static Social Image Prompt Writer — subject to the same wrapper/adapter discipline as §2.4 and §3.

---

## 7. Full Capability-to-Agent Ownership Table

Covers every agent evaluated in this correction, existing and proposed, with all ten required fields.

| Agent | Responsibility | Inputs | Outputs | Preceding stage | Following stage | LLM or deterministic | Runtime or dev/operator-facing | Skill/capability source | Wrapper required | Implementation phase | Current status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `visual_package_director.py` (Stage B) | Provider-facing video prompt writing | Scene skeleton, provider assignment | Scene prompts | VPD Stage A | Video generation (MUAPI) | LLM | Runtime | Existing (`cinematic_prompt_architecture.md`, `veo_translation_skill`, `seedance_translation_skill`) + reconciled Seedance2/Veo Prompt Director (§2.1/§2.2, proposed addition) | Yes (§3) | Current (existing agent); skill reconciliation is proposed | Existing, active |
| `script_engine.py` | Narration beats + visual directions | Narrative package | Script package | Narrative Architect | Humanization (proposed) / VPD / Audio | LLM | Runtime | None today; Humanizer proposed (§4) | Yes (§4) | Current (existing agent); Humanizer integration proposed | Existing, active |
| **Humanization capability (proposed)** | Remove AI-writing tells from narration fields only, preserving facts/names/dates/quotes/evidence levels/uncertainty/narrative intent/schema | `script_package` narration fields | Humanized `script_package` | `script_engine.py` | Audio Agent | LLM | Runtime | Humanizer (§2.5) | Yes (§4) | **Proposed** | NOT INSTALLED |
| `thumbnail_prompt_writer.py` | Phase 1 thumbnail prompt planning | Topic brief, thumbnail intelligence pipeline | Thumbnail prompts | Script Engine (indirect) | MUAPI image generation | LLM | Runtime | Existing (`thumbnail_intelligence_pipeline.compact.md`) + GPT-Image2 reference gallery (proposed addition, §2.4) | Yes, if GPT-Image2 content added | Current (existing agent); skill addition proposed | Existing, active |
| `brand_guardian_agent.py` | Brand/visual compliance QA (Pass 1 + 2) | VPD scenes, thumbnail plans | Compliance verdict | VPD / Thumbnail Director | Generation gate | LLM | Runtime | None today (frozen per project memory) | N/A currently; receives compiled Channel Brand Package only if §5 is implemented | Current (existing agent, frozen); §5 consumer role proposed | Existing, frozen |
| `publishing_agent.py` | Upload approved content to platforms | Pre-built metadata package | Publish confirmation | Publishing Governance gates | — | Deterministic | Runtime | None | No | Current (existing agent) | Existing, active |
| **Channel Creation Wizard (proposed agent)** | Orchestrate new-channel or clone-channel creation | Operator input | Handoff to Brand & Identity Director | Dashboard operator action | Brand & Identity Director | Deterministic (orchestration) | Dev/operator-facing | N/A | N/A | **Proposed — Bucket C** | NOT INSTALLED |
| **Brand & Identity Director (proposed agent)** | Generate or clone-and-differentiate a channel's full visual/editorial identity | Channel Creation Wizard handoff, or clone source | Brand decisions → Brand Compiler | Channel Creation Wizard | Approval gate → Brand Compiler | LLM (differentiation) + deterministic (clone base, via existing `factory_clone.py`) | Dev/operator-facing | UI/UX Pro Max (visual-exploration briefs only, §2.6) | Yes | **Proposed — Bucket C** | NOT INSTALLED |
| **Brand Compiler (proposed agent)** | Compile raw brand decisions into a versioned Channel Brand Package | Approved Brand & Identity Director output | Channel Brand Package | Approval gate | Production consumers, Brand Guardian | Deterministic | Dev/operator-facing | N/A | N/A | **Proposed — Bucket C** | NOT INSTALLED |
| **Content Repurposing Director (proposed agent)** | Verify Master Episode Package facts/scope once for all repurposing | Master Episode Package | Episode Repurposing Brief | Episode completion | Carousel Content Director | LLM (verification) | Runtime | None yet identified | Yes | **Proposed — Bucket D** | NOT INSTALLED |
| **Carousel Content Director (proposed agent)** | Plan carousel structure/copy from the Brief | Episode Repurposing Brief | Carousel content plan | Content Repurposing Director | Carousel Visual Designer | LLM | Runtime | None yet identified | Yes | **Proposed — Bucket D** | NOT INSTALLED |
| **Carousel Visual Designer (proposed agent)** | Define visual direction per carousel slide | Carousel content plan, Channel Brand Package | Visual direction brief | Carousel Content Director | Carousel Image Prompt Writer | LLM | Runtime | UI/UX Pro Max (§2.6) | Yes | **Proposed — Bucket D** | NOT INSTALLED |
| **Carousel Image Prompt Writer (proposed agent)** | Write provider-facing image prompts | Visual direction brief | Image prompts | Carousel Visual Designer | Approved image-provider route (MUAPI) | LLM | Runtime | GPT-Image2 reference gallery (§2.4) | Yes | **Proposed — Bucket D** | NOT INSTALLED |
| **Carousel Compositor (proposed)** | Assemble generated images with text/branding | Generated images, carousel content plan | Final carousel assets | MUAPI generation | Brand Guardian | Deterministic (matches `thumbnail_compositor.py` pattern) | Runtime | N/A | No | **Proposed — Bucket D** | NOT INSTALLED |
| **Fact QA (proposed)** | Verify carousel copy against Brief's verified facts | Carousel content, Episode Repurposing Brief | Fact-check verdict | Brand Guardian | Platform QA | Deterministic or LLM-assisted (undetermined — not scoped by this pass) | Runtime | N/A | N/A | **Proposed — Bucket D** | NOT INSTALLED |
| **Platform QA (proposed)** | Validate per-platform format/size/text-safe-zone compliance | Final carousel assets | Compliance verdict | Fact QA | Publisher | Deterministic | Runtime | N/A | N/A | **Proposed — Bucket D** | NOT INSTALLED |

No existing agent is displaced or duplicated by any proposed entry above — every proposed agent fills a responsibility confirmed absent from live code during this pass (§4's method applied consistently across §5/§6).

---

## 8. Installation Priority Order

1. **Humanizer** → Humanization capability after Script Engine (no blocker, real gap, lowest integration cost)
2. **Seedance2** → VPD Stage B (reconcile via provider-contract adapter)
3. **Veo Prompt Director** → VPD Stage B (reconcile via precedence-governed adapter)
4. **GPT-Image2** (reference gallery only) → Thumbnail Prompt Writer now; Carousel/Static-Social targets contingent on §6's approval
5. **UI/UX Pro Max** (B4.95 dashboard-work use) — available today with no wiring (human-operator Claude Code session tool); its §5-proposed pipeline targets are contingent on §5's approval
6. **Veo Tools** — reference-only; not on the active path; contingent on a future, separately-approved, Finance-Gate-integrated GCP-direct provider decision

This order reflects blocker severity and integration cost, not calendar urgency. None of the six are authorized for implementation by this document, and items 4–6's non-reference-gallery/non-B4.95 targets additionally require §5/§6 approval before any adapter work begins.

---

## 9. Roadmap Placement

**A. Current Foundation / Skills Standardization** (this phase)
- Source verification (complete, §2)
- Decision matrix (this document)
- Upstream manifest, installation design, wrappers, wiring tests — not yet started

**B. Existing top operational priorities** (unchanged by this document; this phase does not move ahead of these)
- YouTube OAuth completion (`docs/roadmap.md:203`, "YouTube OAuth initial authorization")
- Upload Smoke Test (per project memory: Phase 1M complete, upload smoke test passed 2026-06-27)
- Autonomous runtime / worker / scheduler (existing: `run_worker.py`, `hypogeum/scheduler.py`, `hypogeum/queue.py`)
- Autonomous topic discovery (existing: `hypogeum/topic_discovery.py`, `hypogeum/discovery/`)
- Analytics-driven scoring (existing: `hypogeum/analytics_store.py`, `hypogeum/dashboard_analytics.py`, `hypogeum/youtube_analytics.py`)

**C. Future Product Experience and Channel Profile work** (§5, PROPOSED, not yet approved)
- UX/UI Director → corrected to the real "UX/UI Designer" role (B4.95) plus a proposed Brand & Identity Director tier
- Brand & Identity Director (proposed)
- Channel Creation Wizard as an agent (proposed, distinct from the existing UI-flow component)
- Brand Compiler (proposed)
- Multi-channel identity (Channel Brand Package, §5)

**D. Future Content Repurposing work** (§6, PROPOSED, not yet approved)
- Master Episode Package (proposed contract)
- Content Repurposing Director (proposed agent)
- Carousel system (Carousel Content Director, Visual Designer, Image Prompt Writer, Compositor — all proposed)
- Additional platform render targets (static social images, etc. — proposed, unscoped beyond the naming in §2.4)

Buckets C and D are recorded completely per the correction instructions but remain strictly behind Bucket B in execution order — nothing in this document reprioritizes autonomous-runtime work.

---

## 10. Final Source Status Summary

| Source | Status |
|---|---|
| Graphify | **INSTALLED AND ACTIVE — repository-navigation layer** |
| Seedance2 | **VERIFIED / RECONCILIATION CANDIDATE / NOT INSTALLED** |
| Veo Prompt Director | **VERIFIED PACKAGE / SELECTIVE CAPABILITY RECONCILIATION / NOT INSTALLED** |
| Veo Tools | **VERIFIED REPO / CURRENT RUNTIME INCOMPATIBLE / NOT INSTALLED** |
| GPT-Image2 | **VERIFIED REPO / PARTIAL ADOPTION CANDIDATE / NOT INSTALLED** |
| Humanizer | **VERIFIED / STRONG ADOPTION CANDIDATE / NOT INSTALLED** |
| UI/UX Pro Max | **VERIFIED REPO / PARTIAL MULTI-SYSTEM ADOPTION CANDIDATE / NOT INSTALLED** |

---

## 11. Next Safe Step

Await explicit approval of this corrected document, in particular explicit approval of §5 and §6's proposed architecture (which did not exist in any prior document and is new to this pass) separately from §2's upstream-skill decisions (which were already well-grounded before this correction). No adapter, wrapper, prompt change, schema addition, or install command may be written until that approval is given as a separate, subsequent instruction. If approved, the smallest safe first implementation step remains the Humanizer integration-shape decision (§4) — the only item with no dependency on §5/§6's unapproved proposals.

No implementation is authorized by this document.
