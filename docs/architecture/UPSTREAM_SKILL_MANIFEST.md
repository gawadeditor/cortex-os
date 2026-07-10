# Upstream Skill Manifest

**Date:** 2026-07-10
**Status:** CANONICAL — this is the installation manifest for approved external skills, superseded in authority only by `docs/architecture/SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` (approved architectural source of truth) and the two prior audits it built on.
**Method:** Graphify-first orientation, then targeted reads of `PROJECT_CONSTITUTION.md`, `PROJECT_PRINCIPLES.md`, `CURRENT_PHASE.md`, `DECISION_LOG.md`, `docs/architecture/SKILLS_AND_AGENTS_AUDIT.md`, `docs/architecture/SKILL_PROVENANCE_AND_AGENT_MAPPING.md`, `docs/architecture/SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md`, `docs/architecture/GRAPHIFY_FIRST_WORKFLOW.md`, `.gitignore`, current `.claude/skills/` and `hypogeum/skills/` structure, `hypogeum/skill_registry.py`, `hypogeum/prompt_loader.py`, and `tests/test_prompt_files_phase2a.py::TestPromptLoader`. Pinning data (commit SHAs, tags, checksums) below was retrieved via read-only GitHub API calls and a local file hash — no repository was cloned, no package installed, no script executed, per this task's explicit scope.
**No skill is installed by this document.** Every entry below states `NOT INSTALLED` except Graphify, which was installed and verified in a prior, separate task.

---

## 1. Manifest Purpose

- This is the canonical installation manifest for approved external skills. No skill may be installed without a corresponding entry here.
- Upstream content remains verbatim in every installation — no upstream file is ever hand-edited.
- Every source must be pinned to an exact commit SHA or, where no commit history exists (a distributed archive), a content checksum. A moving branch reference (`main`, `HEAD`) is never treated as a permanent pin.
- Every installed file must be checksum-verifiable — SHA-256 recorded here before installation, re-computed and compared after installation.
- Hypogeum-specific behavior remains outside upstream directories, in separate adapter/wrapper files, never inside the pinned upstream tree.
- Installation and runtime wiring are separate changes, tracked as separate atomic commits (§6).
- Graphify must be updated (`graphify update .`) after every installation or wiring change, so the graph reflects the new files before the next session relies on it.

---

## 2. Installation State Definitions

| State | Meaning |
|---|---|
| **VERIFIED** | The source has been confirmed real via an independent check (GitHub API, direct file inspection, or local archive extraction). Authenticity and structure are known. No file has been installed into this repository. |
| **APPROVED FOR INSTALLATION** | A decision matrix (`SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md`) has designated this source for adoption, with a defined target and wrapper requirement. Still no file has been installed. |
| **INSTALLED** | The verbatim upstream file(s) exist on disk in this repository, at the pinned commit/checksum, unmodified. Not yet referenced by any agent. |
| **WIRED** | An adapter/wrapper references the installed file and an agent's `compose_system_prompt()` call includes it, but the wiring has not been test-verified. |
| **TESTED** | The wired integration has a passing test suite covering the required test matrix (§8) for this source. |
| **ACTIVE** | Tested, wired, and currently in production use by the pipeline — the only state authorizing real spend/behavior change from this skill. |
| **REFERENCE ONLY** | The source is retained for human/engineering reference (reading, comparison, future design input) but is explicitly excluded from any current or near-term installation path. Distinct from `RUNTIME INCOMPATIBLE` — a source can be reference-only for scope reasons alone, without being incompatible. |
| **RUNTIME INCOMPATIBLE** | The source's executable mechanism conflicts with Hypogeum's current architecture (e.g. calls a provider directly, bypassing Finance Gate/Provider Router/MUAPI) and cannot be installed as a runtime dependency without a separate, explicitly approved architectural change first. This is a structural finding, not a vague rejection — the specific incompatibility is always named alongside this state. |
| **DEFERRED** | Approved in principle (has a target, per the decision matrix) but intentionally not scheduled in the current installation queue — waiting on a prerequisite (e.g. a future roadmap phase, a license resolution, a portability fix) named explicitly wherever this state is used. |

`BLOCKED` is not used anywhere in this manifest. Every prior use of that word in earlier verification passes is restated here using one of the nine states above, with the actual blocking condition named.

---

## 3. Canonical Source Manifest

### 3.1 Graphify

**Canonical source:** `https://github.com/safishamsi/graphify`
**State: INSTALLED AND ACTIVE**

| Field | Value |
|---|---|
| Installed package name | `graphifyy` (note the double-y — the PyPI/`uv` package name differs from the CLI command name) |
| CLI names | `graphify`, `graphify-mcp` |
| Installed version currently detected | `0.9.11` (confirmed via `graphify --version`, `uv tool list`, and `.claude/skills/graphify/.graphify_version` — all three agree) |
| Project skill path | `.claude/skills/graphify/` (`SKILL.md`, `.graphify_version`, `references/`) |
| Repository exclusion files | `.gitignore` lines 46–49 exclude `graphify-out/` (the generated graph itself is never committed — rebuilt per machine) |
| Graphify-first workflow documents | `docs/architecture/GRAPHIFY_FIRST_WORKFLOW.md` (standing workflow rule), `docs/architecture/GRAPHIFY_VERIFICATION_REPORT.md` (install/build verification), `docs/architecture/GRAPHIFY_INTEGRATION_DESIGN.md` |
| Update command | `graphify update .` (incremental, code-only) or `graphify extract . --code-only` (full rebuild) |
| Verification commands | `graphify --version`; `graphify query "<question>"` against a known-good target to confirm retrieval works |
| Whether hooks are local-only or committed | **Local-only.** `.gitignore` line 15 (`.claude/*`) ignores the entire `.claude/` directory except `.claude/CLAUDE.md` (line 16) and the `.claude/skills/graphify/` subtree (lines 17–25). Per `.gitignore` line 14: "Reinstall local hook enforcement with `graphify install --project`." No git hook, no `settings.json` hook binding, and no MCP server registration is committed — only the skill content itself. |
| Portability requirements for moving Hypogeum to another machine | (1) `uv tool install graphifyy` must be re-run to reinstall the CLI itself — it is not vendored into the repo. (2) `graphify install --project` must be re-run to reinstall project-level hook enforcement, since `.claude/settings.json` is uncommitted (mixes Graphify hooks with machine-specific Octogent hooks, per the `.gitignore` comment). (3) `graphify extract . --code-only` must be re-run to rebuild `graphify-out/graph.json` from scratch — the graph itself is never portable via git. See §9 for the full portability checklist. |

### 3.2 Humanizer

**Canonical source:** `https://github.com/blader/humanizer`
**Decision: APPROVED CANDIDATE — FIRST INSTALLATION TARGET**
**State: NOT INSTALLED**

| Field | Value |
|---|---|
| Source type | Pure-markdown Claude/agent Skill, also packaged as a Claude Code Plugin |
| Version from upstream | `2.8.2` (from `SKILL.md` frontmatter and `.claude-plugin/plugin.json`, both agree) |
| License | MIT |
| Canonical runtime file | `SKILL.md` (34,017 B) — repo's own `AGENTS.md` states this is the sole source of truth; `README.md` and the two `.claude-plugin/*.json` files exist for install/discovery only |
| Official install methods | `npx skills add blader/humanizer`; Claude Code plugin marketplace (`/plugin marketplace add blader/humanizer` + `/plugin install humanizer@humanizer`); manual clone + copy `SKILL.md` |
| Chosen future installation method | **Manual pinned copy** — neither the `npx skills` CLI nor the Claude Code plugin marketplace path pins an exact commit; both resolve against whatever `main` currently points to at install time. A manual copy of `SKILL.md` at a recorded commit SHA is the only method of the three that satisfies this manifest's no-moving-branch pinning requirement (§4). |
| Intended repository destination | `hypogeum/skills/humanizer/SKILL.md` (upstream-verbatim subtree — see §5 for the full layout rationale; not created by this task) |
| Exact target capability | Narration-prose AI-writing-tell removal |
| Target agent/system | A new Humanization capability placed after `hypogeum/agents/script_engine.py` (per `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §4 — no existing agent currently owns final narration prose revision, confirmed via live-code inspection in that document) |
| Wrapper requirement | Yes — a thin adapter translating the upstream skill's conversational framing (voice-calibration prompts, "paste your text here") into Script Engine's unattended pipeline context, per the Decision Matrix §4 constraint list (preserve facts, names, dates, quotes, evidence levels, uncertainty language, narrative intent, schema structure) |
| Prohibited targets | `executive_producer.py`, `research_director.py`, `narrative_architect.py`, `visual_package_director.py`, `shot_diversity_planner.py`, `thumbnail_prompt_writer.py`, `thumbnail_identity_guardian.py`, `brand_guardian_agent.py`, `thumbnail_strategy.py`, `topic_ideator.py` |
| Required tests | See §8 — byte-identity, checksum, license presence, wrapper isolation, prohibited-agent-loading, schema preservation, fact/entity/date/quote preservation, rollback |
| Rollback method | Delete `hypogeum/skills/humanizer/` and the adapter file; no agent's default behavior changes if the wiring commit (§6, step 5) is reverted independently of the installation commit (§6, step 2) |
| Pin | Commit `1b48564898e999219882660237fde01bf4843a0f` (main HEAD as of 2026-06-29, retrieved via GitHub API `2026-07-10`) — no release tag exists upstream, so this commit SHA is the pin |
| State | **NOT INSTALLED** |

### 3.3 Seedance2

**Canonical source:** `https://github.com/dexhunter/seedance2-skill`
**Decision: APPROVED RECONCILIATION CANDIDATE**
**State: NOT INSTALLED**

| Field | Value |
|---|---|
| English skill file | `SKILL.md` (15,337 B, frontmatter `name: seedance-prompt-en`) |
| Chinese skill file disposition | `zh/SKILL.md` (17,450 B) — **not in scope for this installation**. Hypogeum's pipeline and prompts operate in English throughout (confirmed: every internal skill, prompt file, and agent docstring reviewed across this Skills Standardization Phase is English-only); installing the Chinese variant would add an unused file with no consumer. Recorded here so its exclusion is a deliberate decision, not an oversight. |
| License | MIT |
| Exact intended target | `hypogeum/agents/visual_package_director.py` — **Stage B only** |
| Conditional provider-loading requirement | Per `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §3: this content must load into Stage B's composed prompt **only when `provider_router.py` has assigned the scene to a Seedance-routed provider** — not unconditionally alongside Veo content, as today's composition does for the existing internal skill |
| Jimeng vs BytePlus Ark vs MUAPI differences | Upstream targets **Jimeng Seedance 2.0** (ByteDance consumer app, `jimeng.jianying.com`, resolution capped 480p–720p). Hypogeum's production path is **BytePlus Ark** (`hypogeum/byteplus_ark.py`, `BYTEPLUS_ARK_MODEL=seedance-1-lite`) and/or **MUAPI's** Seedance aggregator route — both document a broader range (480p–4K on the full tier per `hypogeum/skills/seedance_translation_skill.md`). These are not interchangeable identities; a blind overwrite of Hypogeum's existing skill with upstream content would silently narrow the documented capability range. |
| Coexistence/reconciliation plan with existing internal Seedance translation skill | **Not a replacement.** `hypogeum/skills/seedance_translation_skill.md` (and its compact runtime version) remains in place. Upstream content is reconciled into it as an additional, explicitly-attributed section — filling the one confirmed gap (`@Reference` role-assignment syntax already partially present under evidence tag `SEED-COMM-01`, worth attributing to this now-known real source) — not overwriting the existing `[L1]`–`[L5]` evidence-tagged content. |
| Wrapper requirement | Yes — a provider-contract adapter resolving which of Hypogeum's configured model identities (BytePlus Ark vs MUAPI Seedance route) the reconciled content actually describes, per Decision Matrix §3's three-layer separation (provider-neutral intent / channel policy / provider translation) |
| Required tests | See §8 — byte-identity, conditional provider loading, duplicate-skill-loading (must not double-inject if both the existing internal skill and reconciled upstream content are present), conflict precedence |
| Pin | Commit `e06c7c63a766d623004a2807881c30685ce517af` (main HEAD as of 2026-02-18, retrieved via GitHub API `2026-07-10`) — no release tag exists upstream |
| State | **NOT INSTALLED** |

### 3.4 Veo Prompt Director

**Canonical source:** `/Users/gawadeditor/Desktop/veo-prompt-director-1.0.0.tar.gz`
**Decision: SELECTIVE CAPABILITY RECONCILIATION CANDIDATE**
**State: NOT INSTALLED**

| Field | Value |
|---|---|
| Archive checksum | SHA-256 `9e7f3600dc4eced97ccc17fcd4dc16e35ed4c2bfb3c2e7233137377991ae170e` (computed directly on the existing local file — 8,075 bytes, no download involved) |
| Package version | `1.0.0` (filename and `metadata.mcpmarket-version` frontmatter agree) |
| File inventory | `SKILL.md` (4,047 B), `references/advanced_workflows.md` (5,382 B), `references/audio_directing.md` (2,787 B), `references/cinematography.md` (5,283 B), `references/prompt_formula.md` (4,863 B) — 5 files total, all pure markdown, no scripts |
| License state | **Absent/unresolved** — no `LICENSE` file in the archive, no license frontmatter field |
| Exact target | `hypogeum/agents/visual_package_director.py` — **Stage B only** |
| Capability-selection requirement | Only the emotion-to-observable-action conversion table, audio-direction syntax cross-check, and multi-shot timestamp structure — per Decision Matrix §2.2, not the full package |
| Precedence policy reference | `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §3, "Deterministic Precedence Policy" — 4-tier conflict resolution (verified factual/physical constraints > channel brand/production policy > provider contract/API constraints > optional upstream creative vocabulary) |
| Static-camera handling | `references/cinematography.md` lists `Static` as a recommended movement, conflicting with `hypogeum/brand/video_style_system.md:345`'s prohibition on static as the primary movement for non-establishing scenes. Per the precedence policy, the adapter must omit this entry or retain it annotated with the exact scope restriction (establishing shots only, max 8s hold) — never present it to Stage B unqualified. |
| Prohibition on modifying package contents | The 5 files are never hand-edited; the capability-selection adapter reads from them but writes its extracted output to a separate Hypogeum-owned file |
| Condition that unresolved license prevents redistribution/extraction | **Binding condition:** because no license is present, this archive's content **must not be redistributed** (e.g. committed verbatim into this repository's git history) and **must not have its text extracted verbatim into any new file** until the license question is resolved with whoever distributed it. The capability-selection adapter may reference *which concepts* to reconciled-adopt (e.g. "add an emotion-to-action table, structured like §X of the source") without reproducing the source's literal prose — this is a stricter constraint than Seedance2/Humanizer, which are both MIT-licensed and redistributable. |
| Portable provenance artifact required before this can move to another machine | **The local Desktop path is not portable — this is a genuine gap, not a formality.** Before Hypogeum can move to another machine (§9) with this source still tracked as `APPROVED RECONCILIATION CANDIDATE`, one of the following must exist: (a) the original mcpmarket listing URL or download page, recorded so the archive can be re-obtained independently of this specific Mac's `~/Desktop/`; (b) the archive's SHA-256 (already recorded above) stored somewhere durable (this manifest) so a re-obtained copy can be verified byte-identical; or (c) if the archive itself is to be preserved, it must be stored in a location this manifest can reference portably (not a per-machine home-directory path) — e.g. a project-internal `third_party/sources/` holding area (not created by this task; see §5). Until one of these exists, this source cannot be verified as unchanged after a machine migration, which is itself a reason to prioritize Seedance2 and Humanizer (both git-hosted, both re-fetchable by commit SHA from anywhere) ahead of this one in the installation queue. |
| Required tests | See §8 — checksum verification (against the archive, not a git commit), license-presence (expected to fail/warn, by design, until resolved), capability-selection isolation, conflict precedence |
| State | **NOT INSTALLED** |

### 3.5 Veo Tools

**Canonical source:** `https://github.com/kdowswell/veo-tools`
**Decision: REFERENCE ONLY / CURRENT RUNTIME INCOMPATIBLE**
**State: NOT INSTALLED**

| Field | Value |
|---|---|
| License | MIT |
| Plugin structure | Claude Code Plugin (`.claude-plugin/marketplace.json` + `plugin.json`), 4 sub-skills: `veo`, `veo-multi-shot`, `veo-setup`, `video-loop` |
| Executable components | `skills/veo/scripts/veo-generate.ts`, `skills/veo-multi-shot/scripts/veo-multi-generate.ts`, `skills/veo-multi-shot/scripts/assemble-clips.sh`, `skills/video-loop/scripts/create-loop.sh` |
| Direct Vertex AI path | Confirmed — all four scripts require a GCP account, `gcloud` CLI, and generated service-account credentials, none of which exist in `hypogeum/config.py` today; calls bypass MUAPI, Provider Router, and Finance Gate entirely |
| No current target agent | Confirmed — no agent in `hypogeum/agents/` is a candidate; the tool's own workflow (`UNDERSTAND → CRAFT → VALIDATE → PRESENT & AWAIT APPROVAL → GENERATE → ITERATE`) is human-approval-gated, structurally incompatible with any unattended pipeline stage |
| Prohibited runtime installation | This source must not be installed as a runtime dependency of any current production agent under any circumstance short of the prerequisite list below being fully satisfied and separately approved |
| Future GCP-direct prerequisites | Per `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §2.3, in full: Identity and Secret Management, Finance Gate, Provider Router, cost tracking, retry and failure policy, observability, autonomous runtime, provider comparison, and configuration/deployment portability |
| Pin (for reference purposes only) | Tag `v1.0.0`, commit `f87f3a9e8531eb91f305722c5b2f5679318268d7` (matches main HEAD as of 2026-01-25) |
| State | **NOT INSTALLED — not in the active installation sequence** |

### 3.6 GPT-Image2 Skill

**Canonical source:** `https://github.com/wuyoscar/GPT-Image2-Skill`
**Decision: PARTIAL ADOPTION CANDIDATE**
**State: NOT INSTALLED**

| Field | Value |
|---|---|
| License | MIT |
| Plugin/repository structure | Claude Code Plugin (`.claude-plugin/`), `skills/gpt-image/SKILL.md` + `references/` (34 files, ~280 KB) + `scripts/generate.py` (launcher) + `src/gpt_image_cli/` (real Python CLI package) |
| **A. Prompt/reference surface** | `references/gallery-*.md` (33 category files: anime, architecture, typography, UI/UX mockups, research-figures, data-visualization, etc.) + `references/craft.md` (prompt-authoring technique guide) — **candidate for adoption** |
| Exact reference files/categories potentially useful | Not the whole gallery unfiltered — the categories overlapping Hypogeum's actual output types: `gallery-typography-and-posters.md`, `gallery-ui-ux-mockups.md` (thumbnail composition relevance), `gallery-photography.md`, `gallery-cinematic-and-animation.md`, `gallery-official-openai-cookbook-examples.md`. The remaining ~28 categories (tattoo design, pixel art, watercolor, ink-and-Chinese, isometric, etc.) have no confirmed Hypogeum consumer and are excluded from the initial selection — this manifest does not select the whole repository for wholesale prompt loading. |
| **B. Direct CLI/OpenAI SDK runtime surface** | `src/gpt_image_cli/cli.py` — imports the official `openai` SDK directly, requires `OPENAI_API_KEY`, bills the user's OpenAI account. **Status: not adopted.** |
| Target systems (prompt/reference surface only) | `hypogeum/agents/thumbnail_prompt_writer.py` (real, existing agent — installable now, per Decision Matrix priority 4); future Carousel Image Prompt Writer (**Decision Matrix §6, PROPOSED, Bucket D — not approved for implementation**); future Static Social Image Prompt Writer (**PROPOSED, Bucket D — not approved**); Brand & Identity Director visual-exploration briefs only (**PROPOSED, Bucket C — not approved**) |
| Direct CLI status | Not adopted — same reasoning as Veo Tools §3.5: bypasses MUAPI/Finance Gate/Provider Router |
| Provider-routing and Finance Gate requirement | Any image generation resulting from a reconciled prompt (Hypogeum-authored, informed by this reference content) still executes through the existing MUAPI `THUMBNAIL_IMAGE_MODEL` route — this source contributes prompt-writing knowledge only, never a generation execution path |
| `gpt-image-2` model-identity discrepancy | **Tracked as a separate, unresolved verification item, independent of this skill's adoption status.** This repo's CLI successfully calls `model="gpt-image-2"` against the real OpenAI API; `hypogeum/config.py:347` currently states "gpt-image-2 is MUAPI-internal only." This contradiction should be resolved by checking OpenAI's own current model catalog directly — out of scope for this manifest. |
| Wrapper requirement | Yes — a curated-selection adapter that copies only the named categories above into a Hypogeum-owned reference file, with attribution to the pinned source, rather than loading the raw upstream `references/` directory wholesale |
| Required tests | See §8 — byte-identity (for the selected subset only), license presence, wrapper isolation (confirming the CLI/`src/` tree is never imported by any Hypogeum module) |
| Pin | Tag `v0.2.0`, commit `74a665173a19e461fa08f84ae76d800f85d2dd1e` — main HEAD has since advanced to `e48b023f38769b6377fe38f1de97197d5e12e6d4` (2026-07-01); the last tagged release is the recommended pin for stability, not the moving `main` branch |
| State | **NOT INSTALLED** |

### 3.7 UI/UX Pro Max

**Canonical source:** `https://github.com/nextlevelbuilder/ui-ux-pro-max-skill`
**Decision: PARTIAL MULTI-SYSTEM ADOPTION CANDIDATE**
**State: NOT INSTALLED**

| Field | Value |
|---|---|
| License | MIT |
| Plugin and sub-skill structure | Claude Code Plugin (`.claude-plugin/`), `skill.json` (top-level manifest, `install: "npx ui-ux-pro-max-cli init --ai {{platform}}"`), 7 sub-skills under `.claude/skills/`: `banner-design`, `brand`, `design-system`, `design`, `slides`, `ui-styling`, `ui-ux-pro-max` |
| Executable scripts | `.cjs` (Node.js): `extract-colors.cjs`, `inject-brand-context.cjs`, `sync-brand-to-tokens.cjs`, `validate-asset.cjs`, `embed-tokens.cjs`, `generate-tokens.cjs`, `validate-tokens.cjs`. `.py` (Python): `fetch-background.py`, `generate-slide.py`, `html-token-validator.py`, `search-slides.py`, `slide_search_core.py` |
| Network-accessing scripts | `fetch-background.py` — scrapes Pexels for slide-background images; no API key required, but performs a live external HTTP request. This is the only confirmed network-accessing script; the remainder operate on local files (token generation, validation, color extraction). |
| Exact potentially useful sub-skills/reference surfaces | `.claude/skills/brand/` (brand guideline template, color-palette management, typography specifications, logo-usage rules — directly relevant to the proposed Brand Compiler input material) and `.claude/skills/design-system/references/` (token architecture, component specs, Tailwind integration — relevant to a future coded-UI consumer, per Decision Matrix §2.6). `banner-design`, `slides`, and the executable script layer are **not** part of the initial useful-surface selection — no current or proposed Hypogeum consumer touches banners or presentation slides. |
| Intended future targets | UX/UI Designer (real, existing role, B4.95) or a future UX/UI Director (**not found anywhere in current repository documentation — see `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` Correction Provenance Note; any reference to it here is to the PROPOSED, not-yet-approved Bucket C role**); Brand & Identity Director (**PROPOSED, Bucket C**); Brand Compiler (**PROPOSED, Bucket C**); Carousel Visual Designer (**PROPOSED, Bucket D**) |
| Explicit prohibition — Brand Guardian | `brand_guardian_agent.py` does not receive the full toolkit under any circumstance. It receives only a compiled, channel-specific Channel Brand Package (per Decision Matrix §5) — never raw UI/UX Pro Max content, reference files, or scripts. |
| Explicit prohibition — unrelated production agents | `executive_producer.py`, `research_director.py`, `narrative_architect.py`, `script_engine.py`, `thumbnail_identity_guardian.py`, `topic_ideator.py`, and every other agent with no coded-UI or brand-authoring responsibility must not receive any part of this toolkit |
| Script-by-script future review requirement | Before any of the 12 executable scripts is ever run in a Hypogeum context, each must be individually reviewed for what it writes, what it reads, and what it calls externally — this manifest authorizes no script execution, and none should be treated as pre-approved merely because the reference-content selection above is |
| Project-scoped installation requirement | If ever installed, this must be a **project-scoped** install (into this repository's own skill tree), never the global `~/.claude/skills/` — matching the pattern already corrected for `ep0-run`/`visual-review` earlier in this Skills Standardization Phase, where project-level content is treated as canonical and global copies as synchronized mirrors, not the reverse |
| Pin | Tag `v2.10.2`, commit `12b486b22e67f5d887962ef8351c1ac863bfaeb9` (matches main HEAD as of 2026-07-06) |
| State | **NOT INSTALLED. No npm installer or executable script is authorized by this task.** |

---

## 4. Pinning and Integrity Policy

Every manifest entry, present and future, must record all of the following fields before it may transition from `VERIFIED` to `APPROVED FOR INSTALLATION`:

| Field | Requirement |
|---|---|
| Canonical URL or portable package source | The exact URL used for verification; for non-git sources, the original distribution location if known, or an explicit note that none is known (as recorded for Veo Prompt Director, §3.4) |
| Repository owner | GitHub org/user login |
| Repository name | Exact repo name |
| Exact commit SHA | Full 40-character SHA — never a branch name, never a short SHA alone (short SHAs may be displayed for readability but the full SHA is the authoritative pin) |
| Release/tag/version | If a tag exists, prefer pinning to the tag's commit over an untagged `main` HEAD (applied above for Veo Tools, GPT-Image2, UI/UX Pro Max; Humanizer and Seedance2 have no tags, so their `main` HEAD commit SHA at verification time is the pin) |
| Retrieval date | The date the pin was established (this manifest: 2026-07-10) |
| License and license file checksum | License type (MIT, etc.) plus a SHA-256 of the `LICENSE` file itself, computed once the source is actually fetched for installation (not computed in this manifest, since fetching is out of scope for this task — recorded here as a required field for the installation commit, §6 step 2) |
| File inventory | Exact list of files being installed (already recorded per source above) |
| SHA-256 per installed file | Computed at installation time for every file actually copied into the repository — not fabricated here, since no file has been fetched under this task's read-only scope |
| Installation method | The exact command/procedure used (manual pinned copy, per §3.2–§3.4's chosen method) |
| Destination | Exact repository path (§5) |
| Upstream update policy | Whether/how this pin is ever advanced — default: never automatically; a version bump is a new, separately-reviewed installation commit, not a silent re-pull |
| Rollback procedure | Named per source above |
| Compatibility notes | Provider-identity distinctions, license constraints, or architectural incompatibilities named per source above |
| Wrapper path | The adapter file's intended location (§5) |
| Target agent | Named per source above |
| Prohibited agents | Named per source above |
| Test suite | Reference to §8's matrix, scoped to what applies to this source |
| Graphify refresh state | Whether `graphify update .` has been run since this source's files were added — tracked per installation commit, not per manifest entry |

**No moving branch may ever be treated as a permanent pin.** Every commit SHA recorded in §3 above was the exact `main` HEAD (or tag) at the moment of retrieval (2026-07-10) — if installation happens later, the SHA must be re-verified as still current or explicitly re-pinned to a newer commit, never silently assumed unchanged.

---

## 5. Repository Layout (design only — not created by this task)

The layout below is evaluated and selected as the safest option; **none of these paths are created by this task.**

```
third_party/skills/<source-name>/<pinned-ref>/    ← untouched upstream source, verbatim,
                                                      one directory per pinned commit/version
                                                      (never edited; a version bump creates a
                                                      new directory, old one retained until the
                                                      bump is confirmed working, then removed in
                                                      its own commit)

hypogeum/skill_adapters/<source-name>_adapter.py  ← Hypogeum-specific wrapper/adapter logic,
                                                      e.g. the Seedance2 provider-contract
                                                      resolver, the GPT-Image2 curated-selection
                                                      extractor, the Humanizer unattended-mode
                                                      translation

hypogeum/skills/compiled/<name>.compact.md        ← compiled/compact runtime artifacts, matching
                                                      the pattern already established by the B1
                                                      token-budget work for veo_translation_skill
                                                      and seedance_translation_skill — mechanically
                                                      derived, never hand-edited independent of
                                                      their source

config/upstream_skills_manifest.json              ← machine-readable mirror of this document's §3
                                                      table (source, pin, checksum, state) — the
                                                      authoritative human-readable record remains
                                                      this markdown file; the JSON is a derived,
                                                      regenerable index for tooling (e.g. a future
                                                      CI check that verifies installed files still
                                                      match their recorded checksums)
```

**Why `third_party/skills/<name>/<ref>/` rather than installing directly into `hypogeum/skills/`:** the current `hypogeum/skills/` directory mixes upstream-adjacent content (`veo_translation_skill.md`, internally authored but modeled on external provider docs) with genuinely internal governance files (`provider_skill_template.md`). A dedicated `third_party/` root makes the verbatim/adapted boundary structurally unambiguous — nothing under `third_party/` is ever a valid edit target, which is a stronger guarantee than a naming convention alone provides. The pinned-ref subdirectory (rather than overwriting in place on every version bump) additionally means a rollback is a directory delete, not a git revert against a file whose history now mixes upstream and Hypogeum commits.

**What should be committed vs. ignored:**

| Path | Committed? | Reason |
|---|---|---|
| `third_party/skills/<name>/<ref>/` (verbatim upstream files) | **Committed** | This is the actual pinned content; without it in git, a fresh clone cannot reproduce the installed state without re-fetching from upstream, defeating the pin's purpose |
| `hypogeum/skill_adapters/*.py` | **Committed** | Hypogeum-authored code |
| `hypogeum/skills/compiled/*.compact.md` | **Committed** | Matches the existing pattern for `veo_translation_skill.compact.md` etc. — mechanically derived but checked in, since regenerating it requires a compaction process, not a trivial rebuild |
| `config/upstream_skills_manifest.json` | **Committed** | Small, human-relevant index |
| Any local download cache, `.tar.gz` archives fetched during installation, `node_modules/` or `.venv/` artifacts from an upstream package's own build tooling | **Ignored** | Regenerable from the pinned source; matches the existing `.gitignore` treatment of `graphify-out/` |

---

## 6. Installation Versus Wiring — Separate Atomic Commits

No installation commit may also modify agent runtime behavior. The sequence, per source, when a source is actually installed:

1. **Provenance manifest** — this document's entry for the source updated to `APPROVED FOR INSTALLATION` (already the state for all six sources as of this manifest; this step is only needed for future new sources)
2. **Verbatim upstream installation** — the pinned files copied into `third_party/skills/<name>/<ref>/`, exactly as retrieved, no modification. Commit touches only that new directory.
3. **Integrity/checksum verification** — a test or script confirming the installed files' SHA-256 matches what's recorded in §3/§4. Commit touches only the checksum record (`config/upstream_skills_manifest.json`) and/or a verification test file.
4. **Thin adapter** — `hypogeum/skill_adapters/<name>_adapter.py` created, referencing the installed files but not yet called by any agent. Commit touches only the new adapter file.
5. **Conditional agent wiring** — the target agent's module updated to call the adapter via `compose_system_prompt()`. This is the only commit in the sequence that touches an existing agent file, and it touches only the import + skill-path composition, never the agent's core logic.
6. **Tests** — the required test matrix (§8) added, covering the specific source's wiring.
7. **Graphify update** — `graphify update .` run, its output (if any files change as a result) committed if applicable.
8. **Smoke test** — a real (non-mocked) invocation of the wired agent confirming the composed prompt is well-formed and the pipeline stage still completes, recorded as evidence in the commit message or a linked test run, not merely asserted.
9. **Activation** — the manifest entry's state updated to `ACTIVE`, and only at this point does the skill affect real pipeline output.

Steps 2–3 may land together in review but remain logically separable (verbatim copy vs. its verification); steps 4–5 must never land in the same commit, since step 5 is the only step with runtime behavioral consequence and needs to be independently revertable from step 4's inert adapter scaffold.

---

## 7. First Installation Recommendation

**Humanizer remains the smallest safe first installation**, confirmed against every criterion:

| Criterion | Humanizer |
|---|---|
| Pure markdown | Yes — confirmed via upstream `AGENTS.md`: "no build step and no code to run" |
| MIT license | Yes — confirmed via GitHub API |
| No executable code | Yes |
| No provider or billing path | Yes — no API key, no external service call of any kind |
| Clear target | Yes — one agent (`script_engine.py`), one new capability step, no ambiguity |
| Easy rollback | Yes — delete two files (installed skill + adapter), revert one wiring commit |
| Measurable effect | Yes — narration text before/after is directly comparable; the 33 named patterns give a concrete pass/fail checklist |
| Risk of changing factual meaning | **The one real risk, and the reason a wrapper is mandatory, not optional.** A prose-rewriting pass can drift facts, dates, quotes, or hedging language if unconstrained. This is exactly why §3.2's wrapper requirement and the required preservation tests (§8) are non-negotiable parts of the installation, not a later hardening pass. |
| Required preservation tests | Fact/entity/date/quote preservation (§8) — comparing named entities and quoted material pre- and post-humanization is the specific test that mitigates the one real risk above |

No other of the six sources scores this cleanly across every criterion — Seedance2 and Veo Prompt Director both require the precedence-policy adapter (§3 of the Decision Matrix) before any content can be safely composed; GPT-Image2 and UI/UX Pro Max both require a curated-selection step across a much larger surface; Veo Tools is excluded from the queue entirely.

**Exact next task after this manifest (not executed here):** design and author the Humanization adapter (`hypogeum/skill_adapters/humanizer_adapter.py`, per §5) and its required test matrix (§8), as a standalone task scoped to installation commit steps 1–6 only (§6) — stopping before step 8 (smoke test) and step 9 (activation), which should each be their own separately-reviewed step given step 9 is the point at which real pipeline output changes.

---

## 8. Required Test Matrix

| Test category | What it verifies |
|---|---|
| Upstream file byte identity | Installed file in `third_party/skills/<name>/<ref>/` is byte-identical to a fresh fetch of the same pinned commit/checksum |
| Checksum verification | SHA-256 of every installed file matches the value recorded in the manifest |
| License presence | A `LICENSE` file exists at the expected path for MIT-licensed sources (Humanizer, Seedance2, GPT-Image2, Veo Tools, UI/UX Pro Max); explicitly expected to be **absent** for Veo Prompt Director, with a test asserting that absence is still correctly flagged, not silently ignored |
| Missing file failure | If a pinned file is deleted or renamed post-install, the adapter fails loudly (matches `prompt_loader.py`'s existing pattern of raising rather than silently degrading — though note `load_skill()` itself currently returns `""` on a missing file; the adapter layer must not inherit that silent-degradation behavior for a pinned upstream file, since a missing pinned file means the pin itself is broken, not an optional skill) |
| Wrapper isolation | The adapter's own code contains no copy-pasted upstream prose beyond what the capability-selection scope explicitly allows (Seedance2/Humanizer: full file, license permits; Veo Prompt Director: concept-reference only, no verbatim reproduction, per §3.4's license constraint) |
| Prohibited agent loading | Every agent named in each source's "prohibited targets" list does not import or reference the adapter, verified by static check or import-graph test |
| Conditional provider loading | Seedance2/Veo content loads into VPD Stage B only when `provider_router.py` has assigned the matching provider to the scene being planned — not unconditionally |
| Token budget | Composed system prompt length after adding the new skill stays within the same order of magnitude as the existing B1 token-budget work's targets (`SKILL_TOKEN_BUDGET_REPORT.md`) — a new skill should not silently reintroduce the ~140K-char bloat that B1 specifically fixed |
| Duplicate skill loading | If both the existing internal skill (e.g. `seedance_translation_skill.compact.md`) and the reconciled upstream addition are present, the composed prompt does not contain the same fact/rule twice |
| Conflict precedence | Where upstream content conflicts with `video_style_system.md` (the `Static`-camera case being the confirmed example), the composed prompt reflects tier 1–2 precedence, never an unqualified upstream instruction |
| Schema preservation | Any package the skill's output feeds into (`script_package`, `visual_package`, etc.) still validates against its existing `hypogeum/schemas.py` schema after the skill is active |
| Fact/entity/date/quote preservation | Specific to Humanizer — named entities, dates, and quoted material extracted from the narration text before and after humanization must match exactly |
| Observability logging | A `skill_manifest`-style structured log event (matching the existing `hypogeum/skill_registry.py::log_loaded_skills()` pattern) fires for every newly-wired skill, listing what loaded and confirming nothing is silently missing |
| Rollback | Reverting the wiring commit (§6 step 5) alone restores prior agent behavior with no other file changes required |
| Graphify indexing | `graphify query` against the new adapter/skill file successfully surfaces it as a node after `graphify update .` |

---

## 9. Portability to Another Machine

| Requirement | Detail |
|---|---|
| What must be committed | `third_party/skills/<name>/<ref>/` (pinned verbatim content), `hypogeum/skill_adapters/*.py`, `hypogeum/skills/compiled/*.compact.md`, `config/upstream_skills_manifest.json`, this manifest document itself, `.claude/CLAUDE.md`, `.claude/skills/graphify/` |
| What must be reinstalled | The `graphify` CLI itself (`uv tool install graphifyy`), project-level hook enforcement (`graphify install --project`), and `graphify-out/graph.json` (rebuilt via `graphify extract . --code-only`) — none of these three are portable via git, confirmed in §3.1 |
| What cannot depend on Desktop paths | **Veo Prompt Director specifically** (§3.4) — its current sole location, `/Users/gawadeditor/Desktop/veo-prompt-director-1.0.0.tar.gz`, is this machine's home directory and will not exist on a new machine. Per §3.4, either a durable download URL or a project-internal holding location (`third_party/sources/`, not yet created) must exist before this source's `APPROVED RECONCILIATION CANDIDATE` status can survive a migration; otherwise the source degrades to `VERIFIED` only (re-verification required) on the new machine. |
| How pinned upstream packages are restored | For git-hosted sources (Humanizer, Seedance2, GPT-Image2, Veo Tools, UI/UX Pro Max): `git clone <repo>` at the pinned commit SHA (or the tag where recorded, §3/§4), re-verify SHA-256 against this manifest, replace the committed `third_party/skills/<name>/<ref>/` copy only if a discrepancy is found (it shouldn't be, since that directory is itself committed and should already be present after a normal `git clone` of Hypogeum) |
| How checksums are verified | Re-run SHA-256 on every file under `third_party/skills/` post-migration and diff against `config/upstream_skills_manifest.json`'s recorded values — any mismatch means either local corruption or an accidental edit, both requiring investigation before continuing |
| How Graphify is restored and rebuilt | `uv tool install graphifyy` → `graphify install --project` → `graphify extract . --code-only` — three commands, none automatic, all must run in this order on first setup at the new machine |
| How local Claude hooks are restored | Same `graphify install --project` step above restores hook enforcement; any Octogent-specific hooks mixed into the uncommitted `.claude/settings.json` (per the `.gitignore` comment, §3.1) are **not** restored by this process and would need their own separate, machine-specific setup — out of scope for this manifest |
| How secrets remain outside git | Not introduced by this manifest — no source in §3 requires a new secret (Humanizer, Seedance2, Veo Prompt Director: none; Veo Tools, GPT-Image2 CLI: require GCP/OpenAI credentials respectively, but both are explicitly `NOT INSTALLED`/`RUNTIME INCOMPATIBLE` and contribute no credential requirement to the active installation queue) |
| How the system proves identical skill state after migration | Running the full test matrix (§8) on the new machine, specifically the "upstream file byte identity" and "checksum verification" tests, is the proof — a passing run confirms the migrated repository's skill state is byte-identical to the pinned state recorded here, not merely "looks the same" |

---

## 10. Final Installation Queue

1. **Humanizer** — `hypogeum/agents/script_engine.py` (via new Humanization capability)
2. **Seedance2 reconciliation** — `hypogeum/agents/visual_package_director.py` Stage B
3. **Veo Prompt Director** — `hypogeum/agents/visual_package_director.py` Stage B, **only after** license/source portability is resolved per §3.4/§9
4. **GPT-Image2 prompt/reference subset** — `hypogeum/agents/thumbnail_prompt_writer.py` (curated categories only, §3.6)
5. **UI/UX Pro Max selected surfaces** — deferred to its future roadmap phase (Decision Matrix Bucket C/D); not scheduled against a current agent
6. **Veo Tools** — excluded from the current installation queue entirely; retained as engineering reference only (§3.5)

**Every item in this queue remains `NOT INSTALLED` as of this manifest.** Only Graphify (§3.1) is `INSTALLED AND ACTIVE`. No file was downloaded, cloned, copied, installed, or executed in the production of this document.

No implementation is authorized by this document.
