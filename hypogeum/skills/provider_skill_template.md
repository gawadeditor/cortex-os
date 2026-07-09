# Provider Prompt Skill — Canonical Template
## Structure and Content Requirements for All Provider Translation Skills

**Document version:** 3.0
**Created:** 2026-07-07
**Updated:** 2026-07-08
**Purpose:** Defines the required structure, required content, required formats, and prohibitions for every provider-specific prompt skill in the Hypogeum pipeline.
**Applies to:** All files matching `hypogeum/skills/*_translation_skill.md`
**Does not apply to:** `cinematic_prompt_architecture.md`, validators, runtime agents, brand guardians

---

## About This Document

A provider translation skill teaches the pipeline how a specific video generation provider interprets prompt language. Its sole responsibility is translation — converting canonical cinematic intent from `video_style_system.md` into the vocabulary and structure that the provider reliably acts on.

This template defines what every such skill must contain, how it must be structured, and what it must never contain.

**Evidence taxonomy — canonical levels:**

| Level | Name | Sources Required | What It Proves |
|---|---|---|---|
| L1 | Provider Capability | Official provider documentation or API specification only | The model has this capability, supports this format, or has this parameter |
| L2 | Documented Prompt Syntax | Official documentation OR official repository | This vocabulary or syntax appears in official documentation — NOT that it produces the intended behavioral effect |
| L3 | Recommended Prompting Practice | Trusted community skills (§7 of this template) only | A community source recommends this approach — NOT that Hypogeum has observed it working |
| L4 | Observed Model Behavior | Hypogeum reproducible experiment with documented ID, prompt, and output | Hypogeum has observed this specific behavior under controlled, repeatable conditions |
| L5 | Hypothesis | None | Internal assumption; may inform workflow as fallback but cannot support production rules |

**Separation rules (never violate):**

- **Never promote L3 → L4.** A community recommendation becomes observed behavior only through a documented Hypogeum experiment with an ID, prompt, output description, and a statement of what the observation demonstrates. Community skills do not confer L4 status regardless of how credible they are.
- **Never promote L2 → behavioral evidence.** Official vocabulary appearing in documentation (L2) proves the terms exist in the spec — it does NOT prove the model reliably executes the intended cinematic effect when the terms are used. Behavioral effectiveness requires L4.
- **L4 items require four documented elements:** (a) experiment ID, (b) prompt used, (c) output description, (d) statement of what the observation demonstrates. Without all four, the observation stays at L5.
- **No production rule may depend on L5 alone.** L5 items can inform workflow as fallback guidance but cannot be the sole basis for a CPA requirement, a channel policy, or a translation rule.

**Core rule:** Every behavioral statement must carry an evidence ID and a level. Behavioral claims without supporting evidence must be labeled `[L5]`. Generic prompting repositories (e.g., `Square-Zero-Labs/video-prompting-skill`) may only be cited in Section B (Prompt Authoring Guidance) — never as evidence of how a specific provider behaves.

---

## 1. Required Document Header

Every provider skill must open with a metadata block before any section content. All fields are required.

```
PROVIDER: [provider name and model identifier, e.g. "Veo 3 / veo3-text-to-video"]
GENERATION_TYPE: [T2V | I2V | T2V+I2V | other — specify]
SKILL_VERSION: [semantic version, e.g. 3.0]
STYLE_SYSTEM_SYNCED: video_style_system.md v[X.Y]
CREATED: [YYYY-MM-DD]
LAST_UPDATED: [YYYY-MM-DD]
HIERARCHY_POSITION: Video Style System → [Provider] Translation Skill → CPA → VBG → Runtime
PARENT_DOCUMENT: hypogeum/brand/video_style_system.md
DOWNSTREAM_DOCUMENT: hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md
```

---

## 2. Required Sections

Every provider skill must contain the following fifteen sections in the following order. Section numbers are fixed. Additional subsections may be added within each section; the required subsection list is a minimum.

### Section 1 — Purpose

**Required content:**

1a. Role statement — translation only, no cinematic rule definition, no creative decisions
1b. What this document is / is not — include all five required bullets (not authority, not runtime, not validator, not prompt template, not CPA)
1c. Translation chain diagram
1d. Traceability principle

---

### Section 2 — Provider Profile

**Required content:** 2a (identification), 2b (production parameters), 2c (quality profile 3–6 bullets), 2d (behavior summary 2–4 sentences)

**PROHIBITED in §2:** Pricing, API authentication, other-provider comparisons by name, canonical cinematic rules.

---

### Section 3 — Evidence Registry

This section is the canonical source list for all behavioral claims in this skill. Every evidence item used in Sections 4, 5, and 6 must be registered here first.

**Required table format:**

| ID | Source Type | Name | URL or Document | Version | Date Accessed | Max Claim Level |
|---|---|---|---|---|---|---|
| [prefix]-DOC-01 | OFFICIAL_DOC | [name] | [url] | [if known] | [YYYY-MM-DD] | L1, L2 |

**ID prefix convention:**
- `VEO-` for Veo evidence items
- `SEED-` for Seedance evidence items
- `KLING-` for Kling evidence items
- `RWY-` for Runway evidence items
- `GEN-` for generic video prompting sources (Section B use only)

**Max Claim Level by Source Type:**

| Source Type | Max Claim Level | Rationale |
|---|---|---|
| `OFFICIAL_DOC` | L1, L2 | Capability facts (L1) and documented syntax (L2) |
| `OFFICIAL_REPO` | L1, L2 | Same as official documentation |
| `COMMUNITY_SKILL` | L3 | Recommendations only — never behavioral evidence |
| `HYPOGEUM_EXP` | L4 (if experiment is documented with ID, prompt, output) | Otherwise L5 |

**PROHIBITED in Evidence Registry:** Evidence IDs labeled `GEN-` (generic prompting repositories) may never be used to support claims in Sections 4, 5A, or 6. They may only be cited in Section 5B.

---

### Section 4 — Verified Provider Behaviour (Section A)

**What belongs here:** Facts about what the model does — API parameters, capability facts, officially documented behaviors. Every item must be directly attributable to official documentation or official repository (sources qualifying for L1 or L2). Community skill claims may appear here only when the community skill explicitly attributes them to official documentation.

**Prohibited here:** Hypogeum observations, hypotheses, general prompting advice, anything from a community skill that is not attributed to an official source.

**Required subsections (all must be present):**

| Subsection | Domain |
|---|---|
| 4.1 | API Parameters and Generation Defaults |
| 4.2 | Supported Durations |
| 4.3 | Supported Aspect Ratios |
| 4.4 | Audio Behavior |
| 4.5 | Officially Supported Camera Vocabulary |
| 4.6 | Officially Supported Prompt Syntax |
| 4.7 | Negative Constraint Format (as documented by provider) |

If a subsection has no official documentation, it must appear with `[NOT OFFICIALLY DOCUMENTED]` and must not contain behavioral claims.

**Required behavioral statement format (all four fields required):**

```
**[B-ID] — [Behavior Name]**

[Description]

- Evidence: [Evidence ID(s)]
- Level: L1 | L2 | L3 | L4 | L5
- §Ref: §[N.N] (canonical section this behavior serves)
```

---

### Section 5 — Prompt Authoring Guidance (Section B)

**What belongs here:** Provider-endorsed techniques for writing effective prompts for this provider. Source types allowed: Official Documentation (L1/L2), Official Repository (L1/L2), Community Skills (L3). Generic video prompting repositories (labeled `GEN-`) are permitted here only.

**Prohibited here:** Hypogeum-only observations, hypotheses without any external basis.

**This section replaces the old §5 Prompt Construction Best Practices.** Content that was previously in "best practices" belongs here if it has any external source, or in Section C if it is purely internal observation.

**Required subsections (minimum):**

| Subsection | Topic |
|---|---|
| 5.1 | Recommended prompt structure or formula |
| 5.2 | Camera language authoring |
| 5.3 | Audio declaration authoring |
| 5.4 | Negative constraint authoring (provider's recommended approach) |

Additional subsections 5.5 and beyond as needed.

Each subsection must state:
- What the guidance is
- The evidence ID and level supporting it
- A concrete example

---

### Section 6 — Hypogeum Production Experience (Section C)

**What belongs here:** Observations, hypotheses, and experiments from Hypogeum's own production use of this provider. This section is the only place where `HYPOGEUM_EXP` or `L5` items may appear without external corroboration.

**Prohibited here:** Claims presented as architectural rules — all items in this section carry reduced authority and are explicitly labeled as L5 (hypothesis) or, if a formal experiment exists, L4.

**Required subsections (minimum):**

| Subsection | Topic |
|---|---|
| 6.1 | Configuration notes (how Hypogeum routes to this provider) |
| 6.2 | Observations from production testing |
| 6.3 | Hypotheses under investigation |

Every item must use the behavioral statement format from §4 with `Level: L5` (or `Level: L4` with experiment ID if formally documented).

**Lifecycle:** When a hypothesis in Section C is confirmed through official documentation or systematic testing, it is promoted to Section A or B with the appropriate evidence ID and level, and removed from Section C.

**L4 promotion requirement:** To promote from L5 to L4, the observation must be documented with: experiment ID, prompt used, output description, and statement of what the observation demonstrates. Informal observation does not qualify.

---

### Section 7 — Translation Rules

Maps each canonical concept from `video_style_system.md` to its expression in this provider's prompt language.

**Required §ref coverage:** §7, §8, §9, §10, §11, §12, §13, §14, §15, §16, §17, §18, §19, §21, §33, §34

**Required content per translation subsection:**
- Canonical statement (cite §ref)
- Evidence basis for the translation approach (must cite Evidence ID and Level from §3)
- Translation table with level marker per row

**If the translation approach is a hypothesis:** Mark the translation table rows with `[L5]` and note which Evidence ID would confirm them.

---

### Section 8 — Rule Traceability Table

A complete cross-reference table showing how every translation rule in §7 traces to evidence and canonical rules.

**Required columns:**

| Rule | Evidence IDs | §Ref | Level |
|---|---|---|---|

**Required coverage:** One row per §ref subsection in §7. Combined rows are permitted where multiple §refs use identical evidence.

**Level values:** `L1` | `L2` | `L3` | `L4` | `L5` | `REQUIRES VERIFICATION`

---

### Section 9 — Prompt Anti-Patterns

Named failure patterns. **Required minimum: 6.** Required types: stacked camera movements, contradictory adjectives, mood vocabulary without physical anchor, generic material description, audio intent omission, at least one provider-specific pattern.

**Required format:**

```
### AP-[NN] — [Name]
**Pattern:** [description]
**Symptom:** [output failure]
**Fix:** [correct approach]
**Evidence:** [Evidence ID] [Level: L1 | L2 | L3 | L4 | L5]
```

---

### Section 10 — Provider Mapping Table

Maps each §ref to its prompt requirement category.

**Required columns:** §Ref | Rule | Prompt Requirement | Explicit Expression Required

**Prompt Requirement values:** `EXPLICIT` | `IMPLICIT` | `SEMI-EXPLICIT` | `PLANNING` | `GENERATION PARAMETER` | `VALIDATION GATE` | `TRANSLATION DISCIPLINE`

---

### Section 11 — Verified Examples

**Required minimum: 4 examples.** Required coverage: lighting (§8), camera movement (§11), material (§16), audio (§21).

**Required format:**

```
### Example [N] — [Domain]: Poor vs. Canonical
**POOR:** > [fragment]
**Why it fails:** [mechanism]
**CANONICAL:** > [fragment]
**Why it works:** [mechanism]
**Evidence:** [Evidence ID] [Level: Lx]
```

---

### Section 12 — Verification Queue

Live tracking of all `REQUIRES VERIFICATION` items. Format:

| # | Behavior | Section | Suggested Verification Method |
|---|---|---|---|

Items removed when confirmed and promoted to the appropriate level; updated in version history.

---

### Section 13 — Failure Modes

Model behavior defects that occur independent of prompt quality. **Required minimum: 6.**

**Required format:**

```
### FM-[NN] — [Name]
**Description:** [what the model does]
**Evidence:** [Evidence ID] [Level: Lx]
**Impact:** [what it breaks canonically]
**Suppression:** [approach + level of suppression evidence]
```

---

### Section 14 — Capability Matrix

**Required columns:** Feature | Status | Notes

Status values: `SUPPORTED [L1]` | `SUPPORTED [L2]` | `SUPPORTED [L3]` | `PARTIAL` | `NOT SUPPORTED [L1]` | `NOT RECOMMENDED [L2]` | `REQUIRES VERIFICATION`

---

### Section 15 — Future Extensions

Provider-specific documentation gaps and priority work. No other-provider stubs permitted (template §6).

---

## 3. Verification Markers

| Marker | Level | When to use |
|---|---|---|
| `[L1: YYYY-MM-DD]` | L1 | Capability confirmed through official documentation or API spec |
| `[L2: YYYY-MM-DD]` | L2 | Syntax or vocabulary confirmed in official documentation or official repository |
| `[L3: YYYY-MM-DD]` | L3 | Practice recommended in a trusted community skill (§7 of this template) |
| `[L4: EXP-ID]` | L4 | Behavior observed in a formally documented Hypogeum reproducible experiment |
| `[L5]` | L5 | Internal assumption — no external evidence |
| `[REQUIRES VERIFICATION: note]` | — | Observation or report exists but insufficient evidence for any level; testing required |
| `[NOT OFFICIALLY DOCUMENTED]` | — | Domain not addressed in any available source |

---

## 4. §Reference Format

Every citation of `video_style_system.md` must use: `§[section number].[subsection number]`

---

## 5. Prohibitions

| Prohibited Content | Why |
|---|---|
| Canonical rules redefined or paraphrased | Only `video_style_system.md` may define canonical rules |
| Pricing, cost, budget data | Runtime/config concern |
| API endpoint details, authentication | Provider payload concern |
| Production-ready prompts | CPA concern |
| L5 items stated as facts | Must carry `[L5]` marker |
| L3 items promoted to behavioral evidence | Community recommendations are not observed behavior |
| L2 items used as behavioral effectiveness evidence | Documented vocabulary ≠ model reliably executes the intended effect |
| Other providers referenced by name within a skill | Each skill is self-contained |
| Generic prompting repositories cited as provider-specific evidence | GEN- sources are Section B only |
| Behavioral claims without evidence IDs and levels | Every claim must trace to §3 with a level |

---

## 6. Section Not Permitted

Future skill stubs for other providers must not appear inside a provider skill file.

---

## 7. Trusted Community Skills (Cited Sources)

The following community skills are pre-approved for citation as L3 sources. Skills not on this list require explicit approval before citation.

| Provider | Repository | Notes |
|---|---|---|
| Seedance | `github.com/dexhunter/seedance2-skill` | Seedance 2.0 skill; attributed to official ByteDance documentation. Max claim level: L3. |
| All (generic) | `github.com/Square-Zero-Labs/video-prompting-skill` | Generic multi-model skill; Section B use only, never as provider-specific evidence |
| Runway | `github.com/runwayml/skills` | Official Runway repository. Max claim level: L1, L2. |
| Kling | `github.com/aedev-tools/kling-3-prompting-skill` | Community skill. Max claim level: L3. |

---

## 8. Registered Provider Skills

| Provider | File | Status | Notes |
|---|---|---|---|
| Veo (Google) | `hypogeum/skills/veo_translation_skill.md` | ACTIVE v3.0 | Template-compliant. Supersedes veo_prompt_skill.md (retired). |
| Seedance | `hypogeum/skills/seedance_translation_skill.md` | ACTIVE v3.0 | Template-compliant. |
| WAN | `hypogeum/skills/wan_translation_skill.md` | NOT YET CREATED | — |
| Kling | `hypogeum/skills/kling_translation_skill.md` | NOT YET CREATED | — |
| Runway | `hypogeum/skills/runway_translation_skill.md` | NOT YET CREATED | — |

---

## 9. Version History

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-07 | Initial canonical template. 9 required sections defined. Verification marker system established. §ref format standardized. Provider registry created. |
| 2.0 | 2026-07-08 | Phase 0.9 evidence-based refactor. Evidence hierarchy established. Evidence Registry section added (§3). Three-section knowledge structure added (§4 Section A, §5 Section B, §6 Section C). Rule Traceability Table added (§8). Behavioral statement format updated to require Source Type, Evidence ID, Confidence, Verification Status, §Ref. Failure Modes (§13) and Capability Matrix (§14) promoted to template-required. Required sections expanded from 9 to 15. Trusted community skills registry added (§7). |
| 3.0 | 2026-07-08 | Phase 1.0 evidence taxonomy refactor. Five-source hierarchy replaced with canonical L1-L5 taxonomy. Behavioral statement format updated: Confidence and Status fields replaced with single Level field. Evidence Registry format updated: Confidence column replaced with Max Claim Level column. Separation rules added (never promote L3→L4; never promote L2→behavioral evidence; L4 requires four documented elements). Verification markers updated to reference levels. Capability Matrix status values updated to reference levels. Registered skills updated to v3.0. |
