# Skills Capability and Agent Decision Matrix

**Date:** 2026-07-10
**Status:** CANONICAL — this document is the source of truth for the next implementation phase. No installation or wiring may proceed until this document is explicitly approved.
**Method:** Graphify-first orientation, then targeted reads of required governance documents, then a scoped Explore-agent survey of adjacent architecture docs (agents, content repurposing, brand system, dashboard/product experience, channel profile, publishing, analytics), with load-bearing claims independently re-verified by direct read. Per `PROJECT_PRINCIPLES.md` §10 ("Never Trust Assumptions"), every decision below is grounded in a cited file, not inferred from a skill's name or reputation.
**Precedes:** `docs/architecture/SKILLS_AND_AGENTS_AUDIT.md` (2026-07-10) and `docs/architecture/SKILL_PROVENANCE_AND_AGENT_MAPPING.md` (2026-07-10) established the agent inventory, internal skill wiring, and the six upstream source verifications this document decides between. This document does not repeat their evidence — it cites them and resolves the open decisions they left pending.

---

## Terminology Reconciliation (read this before the rest of the document)

The word **"skill" has three distinct, unrelated meanings in this repository**, and this document is about only one of them. Conflating them would misapply every decision below.

1. **B5.15a capability-permission skill** (`docs/agent-skills-capability-matrix.md`) — a fixed, 20-entry taxonomy of *tool-use authorities* (`web_search`, `image_generation`, `video_generation`, `model_invoke`, `database_write`, etc.) governing what an agent is permitted to invoke at runtime. This is a **governance/access-control concept**, enforced by the Agent Director reading the registry at run start (`agent-skills-capability-matrix.md:54-56`). Granting or removing one of these requires Owner approval + HIGH-risk routing (`:45-52`, Rule 6).
2. **B6 versioned behavior unit** (`docs/agent-operating-system.md` §7, `:466-540`) — a design-only (not implemented, `:4`) concept of a Module-owned, semver-contracted unit of behavior that "may be a Claude Code skill, a prompt template, a provider API call sequence" (`:32`). This is broader than either of the other two meanings and currently exists only on paper.
3. **Claude Code Skill** (`SKILL.md` files — `.claude/skills/`, `~/.claude/skills/`, `hypogeum/skills/*.md`) — a markdown reference document loaded verbatim (via `compose_system_prompt()` for internal skills, or the Claude Code runtime for user-facing skills) into an LLM's context. **This is the subject of this document, of `SKILLS_AND_AGENTS_AUDIT.md`, of `SKILL_PROVENANCE_AND_AGENT_MAPPING.md`, and of the entire Skills Standardization Phase.**

**Consequence for this document's decisions:** adopting a Claude Code Skill into an agent (e.g. composing Humanizer's content into Script Engine's system prompt) does **not** require a B5.15a Rule 6 capability-grant approval, because it grants no new tool-use authority — it only enriches the content of an already-approved `model_invoke` call. Visual Package Director already holds `video_generation` + `model_invoke` under B5.15a; wiring a Seedance2 or Veo Prompt Director Claude Code Skill into its Stage B system prompt changes *what VPD is told*, not *what VPD is permitted to do*. This document governs the former. B5.15a governs the latter and is untouched by anything below.

---

## 1. Phase Boundary

- **Source verification is complete** for all six reviewed sources: Seedance2 (`https://github.com/dexhunter/seedance2-skill`), Veo Prompt Director (`/Users/gawadeditor/Desktop/veo-prompt-director-1.0.0.tar.gz`), Veo Tools (`https://github.com/kdowswell/veo-tools`), GPT-Image2 Skill (`https://github.com/wuyoscar/GPT-Image2-Skill`), Humanizer (`https://github.com/blader/humanizer`), UI/UX Pro Max (`https://github.com/nextlevelbuilder/ui-ux-pro-max-skill`). All six verifications are recorded in `docs/architecture/SKILL_PROVENANCE_AND_AGENT_MAPPING.md` §6–§9 (plus the UI/UX Pro Max verification delivered in chat, not yet filed — see §7 below).
- **No external skill is installed yet, except Graphify** — which is the repository-navigation layer (`docs/architecture/GRAPHIFY_VERIFICATION_REPORT.md`), not a content skill subject to this matrix, and is out of scope for these decisions.
- **This phase decides capabilities, ownership, order, and integration boundaries only.** It does not touch any agent, prompt, provider client, or dashboard file.
- **Installation and wiring happen only after this document is approved.** Approval is a separate, explicit future action — this document's existence is not that approval.
- **Upstream skills must remain verbatim.** Where adopted, the original `SKILL.md` (or equivalent) content is installed unmodified. No upstream file is ever hand-edited in place.
- **Hypogeum-specific behavior must live in thin wrappers, adapters, compiled contracts, or orchestration layers** — separate files that sit alongside the verbatim upstream content, never inside it. This matches the existing internal pattern already established in `hypogeum/skill_registry.py` (path/status/wiring metadata layered over static skill files it never edits).
- **Claude must never recreate, paraphrase, or imitate an upstream skill as a replacement.** If a source is not adopted verbatim, it is either left unused or represented only by a citation to its real URL — never by a hand-written approximation of its content.

---

## 2. Final Upstream Decision Matrix

### 2.1 Seedance2

| Field | Value |
|---|---|
| Canonical source | `https://github.com/dexhunter/seedance2-skill` |
| Artifact type | Claude Skill (pure markdown, bilingual EN/ZH) |
| License | MIT |
| Executable components | None |
| Useful capabilities | `@Reference` role-assignment syntax, input-limit tables (images/video/audio), duration/aspect-ratio ranges — verbatim-matching content already partially present in `hypogeum/skills/seedance_translation_skill.md` under evidence tag `SEED-COMM-01` |
| Incompatible components | Targets **Jimeng Seedance 2.0** (ByteDance consumer app, resolution capped 480p–720p); Hypogeum's production path is **BytePlus Ark** / **MUAPI**, whose documented range is broader (480p–4K on the full tier) — a blind overwrite would narrow Hypogeum's documented capability |
| **Final decision** | **RECONCILE AND ADOPT FOR PROVIDER-PROMPT CAPABILITIES** |
| Exact target system/agent | `hypogeum/agents/visual_package_director.py` — **Stage B (provider-facing video prompt writing) only** |
| Agents that must NOT receive it | `executive_producer.py`, `research_director.py`, `narrative_architect.py`, `script_engine.py`, `shot_diversity_planner.py`, `thumbnail_prompt_writer.py`, `thumbnail_identity_guardian.py`, `brand_guardian_agent.py`, `thumbnail_strategy.py`, `topic_ideator.py` |
| Wrapper required | **Yes** — a provider contract that keeps Jimeng, BytePlus Ark, and MUAPI's `seedance-1-lite`/aggregator model identities distinct, resolving which of Hypogeum's already-configured model slugs (`BYTEPLUS_ARK_MODEL`, MUAPI Seedance routing) the reconciled content actually describes. Install upstream verbatim later, once this adapter exists; do not blindly replace `hypogeum/skills/seedance_translation_skill.md`. |
| Installation priority | 2 |
| Current status | **NOT INSTALLED** |

### 2.2 Veo Prompt Director

| Field | Value |
|---|---|
| Canonical source | `/Users/gawadeditor/Desktop/veo-prompt-director-1.0.0.tar.gz` |
| Artifact type | Skill package (mcpmarket-distributed), pure markdown (`SKILL.md` + 4 `references/` files) |
| License | **None found** — no `LICENSE` file, no license frontmatter field |
| Executable components | None |
| Useful capabilities | Universal Prompt Formula, **emotion-to-observable-action conversion table** (confirmed absent from Hypogeum's existing `veo_translation_skill.md`), audio-direction syntax (`SFX:`/`Ambient noise:` — already present in Hypogeum's skill, corroborating overlap), multi-shot timestamp structure, camera/composition vocabulary where compatible with Hypogeum's brand rules |
| Incompatible components | `references/cinematography.md` lists `Static` as a recommended camera movement ("Static camera, locked off wide shot") |
| **Final decision** | **RECONCILE SELECTED CAPABILITIES; DO NOT INSTALL AS AN UNFILTERED RUNTIME PROMPT** |
| Exact target system/agent | `hypogeum/agents/visual_package_director.py` — **Stage B only** |
| Agents that must NOT receive it | Same list as §2.1 |
| Wrapper required | **Yes** — a thin capability-selection adapter that extracts only the compatible sections (emotion-to-action table, audio syntax cross-check, multi-shot structure) without copying or rewriting the source file itself, governed by the precedence policy in §3 below |
| Installation priority | 3 |
| Current status | **NOT INSTALLED** |

**Conflict-handling clarification (per explicit instruction):** the upstream `Static`-camera recommendation is **not itself a reason to reject this source**. The real issue is two instructions reaching the same LLM call with no resolution order. See §3, "Deterministic Precedence Policy," for how this is resolved without editing the upstream package. Also note (verified directly, `hypogeum/brand/video_style_system.md:345`): Hypogeum's actual rule is narrower than "never static" — it forbids **"static hold as the primary camera movement for non-establishing scenes"**; a controlled hold up to 8 seconds is explicitly permitted for establishing shots (`:339`, `:352-354`). The precedence policy must encode this precise rule, not a flattened "no static ever" version of it.

### 2.3 Veo Tools

| Field | Value |
|---|---|
| Canonical source | `https://github.com/kdowswell/veo-tools` |
| Artifact type | Claude Code Plugin (4 skills) with real executable automation |
| License | MIT |
| Executable components | `skills/veo/scripts/veo-generate.ts`, `skills/veo-multi-shot/scripts/veo-multi-generate.ts`, `skills/veo-multi-shot/scripts/assemble-clips.sh`, `skills/video-loop/scripts/create-loop.sh` — all call **Google Vertex AI directly** via `gcloud`/service-account credentials |
| Useful capabilities | 5-Element Formula (redundant with Hypogeum's existing content), a documented human-approval-gated generation workflow (not applicable to Hypogeum's unattended pipeline) |
| Incompatible components | The entire executable layer — direct Vertex AI calls, GCP billing, human-approval-gated workflow |
| **Final decision** | **DO NOT USE AS A CURRENT PIPELINE RUNTIME PROVIDER PATH** |
| Exact target system/agent | None |
| Agents that must NOT receive it | All pipeline agents — `visual_package_director.py`, `shot_diversity_planner.py`, `thumbnail_prompt_writer.py`, `thumbnail_identity_guardian.py`, `brand_guardian_agent.py`, `executive_producer.py`, `research_director.py`, `narrative_architect.py`, `script_engine.py`, `thumbnail_strategy.py`, `topic_ideator.py` |
| Wrapper required | N/A under current scope. **If** a future, separately-approved GCP-direct provider path is pursued, that path must first integrate with the **Finance Gate** — i.e. any GCP-direct generation call must be authorized through the same budget-authorization step (`FinancePostVPDBlockedError`, confirmed present in the codebase) that currently gates every MUAPI call, before Provider Router or Cost Tracking would ever see a GCP-sourced clip. No script from this repo may be attached to any production agent before that integration exists. |
| Installation priority | 5 (reference-only; not on the active adoption path) |
| Current status | **NOT INSTALLED** |

**Clarification (per explicit instruction):** this does not mean the repo is bad. Its direct-execution scripts must not be attached to any current production agent because they would create a parallel generation route outside `Finance Gate → Provider Router → MUAPI/provider adapter → Cost Tracking`. It may be retained as an **engineering reference** for a future, separately-approved GCP-direct provider integration — a distinct architectural decision, out of scope for the current Skills Standardization Phase.

### 2.4 GPT-Image2 Skill

| Field | Value |
|---|---|
| Canonical source | `https://github.com/wuyoscar/GPT-Image2-Skill` |
| Artifact type | Hybrid — prompt/reference gallery (34 markdown files) + agentic skill + standalone Python CLI, packaged as a Claude Code Plugin |
| License | MIT |
| Executable components | `src/gpt_image_cli/cli.py` — imports the official `openai` SDK directly (`OpenAI()`, `client.images.generate/edit`), requires `OPENAI_API_KEY`, bills the user's OpenAI account directly |
| Useful capabilities | 33 curated `references/gallery-*.md` category files (typography, UI/UX mockups, research-figures, etc.) — **a genuine capability gap**: Hypogeum has no comparable curated image-prompt example library today |
| Incompatible components | The CLI's direct-OpenAI-SDK execution path — same category of architectural bypass as Veo Tools §2.3, but against OpenAI rather than Vertex AI |
| **Final decision** | **SPLIT DECISION: reference-gallery content is a reconcile-later candidate; the executable CLI is DO NOT USE, same reasoning as Veo Tools** |
| Exact target system/agent | If reference content is ever adopted: `hypogeum/agents/thumbnail_compositor.py` / `thumbnail_director.py` planning stage (image generation only — this repo has no video-generation content) |
| Agents that must NOT receive it | All non-thumbnail agents, plus the CLI/script components must reach no agent at all |
| Wrapper required | Yes, if the reference-gallery portion is ever adopted — same pattern as §2.1/§2.2. Not applicable to the CLI portion, which is not adopted. |
| Installation priority | 4 |
| Current status | **NOT INSTALLED** |

**Open finding requiring separate resolution, independent of this skill's adoption status:** this repo's CLI hardcodes and successfully calls `model="gpt-image-2"` against the real OpenAI API. Hypogeum's own `hypogeum/config.py:347` currently states `gpt-image-2 is MUAPI-internal only` — a claim this widely-used (3,626-star), actively-maintained external tool directly contradicts. This is a **documentation-accuracy question for Hypogeum's own config comments**, not a skill-adoption question, and should be tracked and resolved separately from this matrix.

### 2.5 Humanizer

| Field | Value |
|---|---|
| Canonical source | `https://github.com/blader/humanizer` |
| Artifact type | Portable Claude/agent Skill (pure markdown), also packaged as a Claude Code Plugin |
| License | MIT |
| Executable components | None — confirmed via the repo's own `AGENTS.md`: "There is no build step and no code to run" |
| Useful capabilities | 33 numbered AI-writing-tell patterns with before/after examples, an optional voice-calibration step, a draft→audit→final-rewrite loop |
| Incompatible components | None found — no content conflict with any existing Hypogeum rule |
| **Final decision** | **STRONG ADOPTION CANDIDATE — RECONCILE INTEGRATION SHAPE, THEN ADOPT VERBATIM** |
| Exact target system/agent | `hypogeum/agents/script_engine.py` only — its narration-beat output is real human-facing prose (becomes spoken documentary narration via ElevenLabs, confirmed via `graphify query` in the prior verification) |
| Agents that must NOT receive it | `executive_producer.py`, `research_director.py`, `narrative_architect.py`, `visual_package_director.py`, `shot_diversity_planner.py`, `thumbnail_prompt_writer.py`, `thumbnail_identity_guardian.py`, `brand_guardian_agent.py`, `thumbnail_strategy.py`, `topic_ideator.py` |
| Wrapper required | Yes, per Installation Policy — a thin adapter to translate the upstream skill's conversational framing (voice-calibration prompts, "paste your text here") into Script Engine's unattended pipeline context; the upstream `SKILL.md` itself is never edited |
| Installation priority | 1 (no architectural blocker, no content conflict, fills a confirmed real gap — the lowest-risk, highest-value candidate of the six) |
| Current status | **NOT INSTALLED** |

### 2.6 UI/UX Pro Max

| Field | Value |
|---|---|
| Canonical source | `https://github.com/nextlevelbuilder/ui-ux-pro-max-skill` |
| Artifact type | Large multi-skill toolkit (7 sub-skills: `banner-design`, `brand`, `design-system`, `design`, `slides`, `ui-styling`, `ui-ux-pro-max`), packaged as a Claude Code Plugin with an npm-published CLI installer |
| License | MIT |
| Executable components | Multiple `.cjs` (Node.js) and `.py` scripts per sub-skill, including a Pexels web-scrape (`fetch-background.py`, no API key) and a slide/design-token generation engine |
| Useful capabilities | None mapped to any confirmed Hypogeum gap — see incompatibility below |
| Incompatible components | No architectural integration point exists: Hypogeum's dashboard is confirmed **purely Flask/deterministic** (no LLM-calling "dashboard agent" — Dashboard Director's `model_invoke` is optional/unconfirmed per `docs/agent-skills-capability-matrix.md:1022`, "for natural-language health summaries if configured"), and channel-profile brand values are deterministically operator-entered, not LLM-generated (`docs/channel-profile-system.md` §5). This repo's domain (coded React/Tailwind/HTML component generation) has no Hypogeum consumer today. |
| **Final decision** | **NOT ADOPTED — NO CURRENT INTEGRATION POINT; PROVENANCE ALSO FLAGGED** |
| Exact target system/agent | None |
| Agents that must NOT receive it | All — no agent in the pipeline touches coded UI |
| Wrapper required | N/A — nothing to wrap |
| Installation priority | 6 (lowest; deferred pending an explicit, separate product decision on whether Hypogeum ever wants dashboard-facing AI design-system tooling) |
| Current status | **NOT INSTALLED** |

**Provenance caution (carried from the original verification, restated here for the decision record):** this source's popularity profile — ~104K stars via an organization account created 7 months prior to verification, heavy cross-tool topic-tag stuffing, a dedicated marketing site — is atypical relative to the other five sources and was flagged as worth independent trust-verification before being relied upon as a reference, separate from the architectural-fit finding above.

---

## 3. Deterministic Precedence Policy (Visual Package Director Stage B)

This policy exists because Stage B already composes multiple documents into one system prompt (`cinematic_prompt_architecture.md` + `visual_language_engine.compact.md` + `veo_translation_skill.compact.md` + `seedance_translation_skill.compact.md`, per `SKILL_PROVENANCE_AND_AGENT_MAPPING.md` §2), and any reconciled Seedance2/Veo Prompt Director content would join that same composition. The policy below is a rule for the *adapter layer* (§2.1/§2.2's "wrapper required"), not a runtime code change — it is documented now so that whoever builds the adapter has an unambiguous resolution order to implement.

**Precedence, highest to lowest:**

1. **Hypogeum brand/production visual rules** — `hypogeum/brand/video_style_system.md` §11 (camera narrative-function table, one-movement-per-scene rule, the five explicit forbidden behaviors at `:345-350`, the 8-second/16-second hold limits), `hypogeum/brand/visual_design_principles.md` (20 principles, prioritized Viewer Trust > Historical Credibility > Perceived Realism > Curiosity Generation), and `hypogeum/brand/thumbnail_style_system.md` for thumbnail-adjacent work. These are channel/production-level rules and always win.
2. **Hypogeum's existing internal cinematic policy** — `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md` (the 9-layer spec, including its own restatement of the no-bare-static rule and the `[no vignette]`/`[lamp source off-frame]` tag requirements).
3. **Hypogeum's existing internal provider-translation skills** — `veo_translation_skill.md`/`seedance_translation_skill.md` (compact versions), which already carry `[L1]`–`[L5]` evidence tagging and `REQUIRES VERIFICATION` flags.
4. **Reconciled upstream capability extracts** (§2.1/§2.2's adapter output) — only content confirmed non-conflicting with tiers 1–3 may be included here at all. Where an upstream document offers optional vocabulary tier 1–3 doesn't forbid but also doesn't require, it may supplement; where it conflicts, it is dropped by the adapter before composition, not overridden at prompt-injection time.

**Concrete application to the `Static`-camera case:** tier 1 forbids static *as the primary camera movement for non-establishing scenes*, and separately caps establishing-shot holds at 8 seconds. The adapter must therefore either omit the upstream `Static` entry entirely from what it extracts, or — if retained for completeness — annotate it with tier 1's actual scope restriction so Stage B never receives an unqualified "Static is a valid choice" instruction. This is an adapter-construction requirement, not a runtime prompt-engineering task, and is not authorized for implementation by this document.

---

## 4. Capability-to-Agent Architecture

This section cross-references the agent inventory established in `SKILLS_AND_AGENTS_AUDIT.md` §3 against the findings of this pass's architecture survey (agents, content repurposing, brand system, dashboard, channel profile, publishing, analytics). It does not duplicate `docs/agent-skills-capability-matrix.md` (the B5.15a capability-permission registry, a different "skill" meaning per the Terminology Reconciliation above) — it cites it only where directly relevant to a Claude-Code-Skill decision.

| Capability domain | LLM-calling agent(s) | Claude Code Skill relevance | Finding this pass |
|---|---|---|---|
| Video prompt writing | `visual_package_director.py` (Stage B), `shot_diversity_planner.py` | Seedance2, Veo Prompt Director (§2.1, §2.2) | Confirmed active target; adapters required before adoption |
| Thumbnail/image prompt writing | `thumbnail_prompt_writer.py`, `thumbnail_strategy.py` | GPT-Image2 reference gallery only (§2.4) | Low-priority candidate; no urgency |
| Narration prose quality | `script_engine.py` | Humanizer (§2.5) | Highest-priority candidate; no blocker |
| GCP-direct video generation | None currently | Veo Tools (§2.3) | Explicitly not a current target; Finance Gate integration is a hard precondition for any future reconsideration |
| Coded UI/dashboard design | **None** — confirmed via this pass's survey that the dashboard is purely Flask/deterministic (`docs/dashboard-product-architecture.md`; Dashboard Director's `model_invoke` is optional/unconfirmed, `docs/agent-skills-capability-matrix.md:1022`) | UI/UX Pro Max (§2.6) | No integration point exists; not adopted |
| Content repurposing (Shorts) | **None new** — `docs/shorts_extraction_architecture.md` is a design proposal (not implemented, status "awaiting approval," `:3`) that adds prompt-level fields to existing VPD/Script Engine output; the actual crop/concat step is deterministic FFmpeg in `assembly_agent.py`, not an LLM call | None of the six sources map here | No decision needed; flagged for awareness only, since any future Shorts-specific visual-prompt need would route through the same VPD Stage B target as §2.1/§2.2 |
| Channel/brand profile creation | **None** — confirmed deterministic: profile values are operator-entered structured fields, injected as prompt prefixes into VPD/Thumbnail Director (`docs/channel-profile-system.md` §5, `:507-534`) | None of the six sources map here | No decision needed |
| Publishing | **None** — confirmed zero `model_invoke` for the Publishing Agent (`docs/agent-skills-capability-matrix.md:751-767`); Publishing Agent only uploads a pre-built metadata package it cannot modify (`docs/publishing-governance.md:402-442`) | None of the six sources map here | No decision needed |
| Analytics | **None at the collection layer** — Analytics Collection Agent is explicitly non-synthesizing (`docs/agent-skills-capability-matrix.md:838`); `model_invoke` exists only in the separate, **not-yet-implemented** Intelligence Agents (Cost/Quality/Audience Intelligence) layer downstream | None of the six sources map here | No decision needed; Intelligence Agents are a future implementation target outside this matrix's scope |
| Social content | **No dedicated system exists** — confirmed via directory search that no `docs/` file addresses "social content" as a distinct concern; closest adjacent concept is Shorts extraction (above) | None of the six sources map here | No decision needed |

---

## 5. Installation Priority Order

1. **Humanizer** → Script Engine (no blocker, real gap, lowest integration cost)
2. **Seedance2** → VPD Stage B (reconcile with existing internal skill via provider-contract adapter)
3. **Veo Prompt Director** → VPD Stage B (reconcile selected capabilities via precedence-governed adapter)
4. **GPT-Image2** (reference gallery only) → Thumbnail Director/Compositor planning (low urgency)
5. **Veo Tools** — reference-only; no installation path under current architecture; contingent on a future, separate Finance-Gate-integrated GCP-direct provider decision
6. **UI/UX Pro Max** — not adopted; no current integration point; deferred pending a separate product decision

This order reflects blocker severity and integration cost, not calendar urgency — none of the six are authorized for implementation by this document.

---

## 6. Next Safe Step

Await explicit approval of this document. No adapter, wrapper, prompt change, or install command may be written until that approval is given as a separate, subsequent instruction. If approved, the smallest safe first implementation step is the Humanizer integration-shape decision named in §2.5 (choosing between system-prompt injection vs. a separate post-generation revision pass) — the lowest-risk item on the priority list, and the only one with no adapter-design dependency on the precedence policy in §3.

No implementation is authorized by this document.
