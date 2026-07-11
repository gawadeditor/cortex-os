# Video Prompt Skills — Ownership and Attachment Audit

**Date:** 2026-07-11
**Status:** READ-ONLY AUDIT — no production code, test, or skill file was modified or installed to produce this document.
**Method:** Graphify-first orientation (four queries, budgets 2000/1800/1800/1600 — the local graph has no LLM enrichment configured, so results were raw BFS node listings used for file discovery; findings below are grounded by direct reads of the files Graphify surfaced), then direct reads of `hypogeum/agents/visual_package_director.py` (full constants/imports section, all six `run_agent()` call sites and their surrounding context), `hypogeum/agents/scene_planner.py`, `hypogeum/agents/shot_diversity_planner.py`, `hypogeum/agents/provider_router.py` (full file), `hypogeum/muapi.py` (targeted: `submit_scene()`, `generate_thumbnail()`, LLM-call absence check), `hypogeum/agents/video_generation_agent.py` (targeted), `hypogeum/prompts/vpd_scene_prompts.txt` (targeted), `hypogeum/skill_registry.py` (`veo_translation_skill`/`seedance_translation_skill` entries), `config/upstream_skills_manifest.json`, `docs/architecture/UPSTREAM_SKILL_MANIFEST.md` §3.3–§3.5 (the original, separate verification pass for these exact three sources, dated 2026-07-10 — cited extensively below rather than re-derived, since re-verifying it against the current repository state confirmed no drift).

**Important correction to the task's assumed file list:** `hypogeum/agents/shot_director.py` and `hypogeum/agents/prompt_director.py` **do not exist** — confirmed by direct directory listing. `hypogeum/provider_router.py` also does not exist at that path; the real file is `hypogeum/agents/provider_router.py`. This audit does not assume the task's file list was complete or accurate, per its own instruction, and traces ownership from the actual current codebase instead.

---

## 1. Current Video Prompt Call Graph

```
run_pipeline() → _run_visual_package_director() → run_visual_package_director()
                                                    [hypogeum/agents/visual_package_director.py:272]

  Stage A1 — META_PROMPT (compose_system_prompt(vpd_skeleton_meta.txt))
    run_agent(...)                                    [VPD.py:374]
    → entities, palettes, thumbnail-brief metadata. No prompt text written.

  Stage A2 — SCENE_BATCH_PROMPT (compose_system_prompt(vpd_skeleton_scene_batch.txt))
    run_agent(...) per batch                          [VPD.py:442]
    → scene skeleton (visual_spec fields: subject, setting, action). No prompt text.

  Stage A4 — PRESENTATION_PLANNER_PROMPT
    (compose_system_prompt(vpd_scene_presentation_planner.txt, visual_language_engine.compact.md))
    run_agent(...) per batch                          [VPD.py:569]
    → presentation_method planning. No prompt text.

  Shot Diversity Planner — separate module, own run_agent() call
    [hypogeum/agents/shot_diversity_planner.py]
    _load_prompt() = compose_system_prompt(shot_diversity_planner.txt, visual_language_engine.compact.md)
    run_agent(...)                                    [shot_diversity_planner.py:138]
    Called from VPD.py:650, between Stage A4 and Stage B.
    → shot_scale, camera_movement, camera_angle, composition, color_palette,
      environment, subject, visual_motif, reuse_allowed — shot-DESIGN constraints
      for Stage B to honor. No prompt text written.

  Stage B — SCENE_PROMPTS_PROMPT  ★ THE FINAL PROMPT WRITER ★
    (compose_system_prompt(vpd_scene_prompts.txt, cinematic_prompt_architecture.md,
                            veo_translation_skill.compact.md, seedance_translation_skill.compact.md))
    run_agent(...) per batch                          [VPD.py:747]
    run_agent(...) retry (same SCENE_PROMPTS_PROMPT, smaller batch on truncation)
                                                       [VPD.py:1328]
    → writes scene["production_spec"]["provider_routing"]["primary_prompt"]
      and ["fallback_prompt"] — the exact strings later submitted to providers.
      hypogeum/prompts/vpd_scene_prompts.txt:36 states this explicitly:
      "primary_prompt — a full 9-layer cinematic prompt following the
      Cinematic Prompt Architecture skill appended below. This is passed
      directly to the MUAPI video generation model."

  Visual Intent QA — VISUAL_INTENT_QA_PROMPT
    run_agent(...) per batch                          [VPD.py:1205]
    → scores already-written prompts against visual intent. Does not write
      or rewrite prompt text.

  ↓ (deterministic, zero LLM calls from here on)

  provider_router.build_scene_spec(scene, entity_hosted_urls)
    [hypogeum/agents/provider_router.py:61]
    → reads routing.get("primary_prompt", ""), routing.get("fallback_prompt", "")
      (already written by Stage B) and normalizes tier values (normalize_tier()).
      Own docstring: "Not responsible for: ... Prompt writing (VPD / prompt files)."
      Confirmed zero run_agent/call_claude references in this file.

  video_generation_agent.generate_scene_video() / generate_all_scene_videos()
    [hypogeum/agents/video_generation_agent.py]
    → orchestrates muapi.submit_scene() calls. Confirmed zero
      run_agent/call_claude references in this file.

  muapi.submit_scene() → provider API (Veo/Seedance/etc., tier-routed)
    [hypogeum/muapi.py:569]
    → deterministic HTTP submission of the already-written primary_prompt/
      fallback_prompt strings. Confirmed zero run_agent/call_claude
      references anywhere in muapi.py.

  (Thumbnail path, unrelated to this audit's video-generation scope, follows
   the same pattern: thumbnail_prompt_writer.py → muapi.generate_thumbnail())
```

---

## 2. Provider Paths

There is **no separate "Veo direct" or "Seedance direct" code path** distinct from the MUAPI aggregator route — confirmed by `docs/architecture/UPSTREAM_SKILL_MANIFEST.md` §3.3 ("Hypogeum's production path is **BytePlus Ark** ... and/or **MUAPI's** Seedance aggregator route") and by this audit's own reading: `hypogeum/muapi.py` is the single execution module `video_generation_agent.py` calls, and `provider_router.py`'s `normalize_tier()`/`build_scene_spec()` route by **tier** (T1/T2/T3, per `CLAUDE.md`'s own frozen tier table), not by a hardcoded "Veo" or "Seedance" branch. Both providers are reached through the same `submit_scene()` function with different `production_spec` tier assignments.

| | Veo (all tiers) | Seedance (BytePlus Ark route, referenced separately) | MUAPI (aggregator, all tiers) |
|---|---|---|---|
| Source package | `scene["production_spec"]["provider_routing"]` | same | same |
| Final prompt field | `primary_prompt` / `fallback_prompt` | `primary_prompt` / `fallback_prompt` | `primary_prompt` / `fallback_prompt` |
| Writer function | `write_thumbnail_prompts`-equivalent for video: **VPD Stage B** (`run_agent()` at `VPD.py:747`, composing `veo_translation_skill.compact.md`) | **VPD Stage B** — the *same* call, composing `seedance_translation_skill.compact.md` in the *same* system prompt | **VPD Stage B** — identical; MUAPI is the transport, not a separate prompt owner |
| Validators | Stage B's own schema validation (`visual_package` schema, via `run_agent()`); no separate provider-specific prompt validator module was found | same | same |
| Router | `provider_router.build_scene_spec()` (tier-based, deterministic) | same | same |
| Execution function | `video_generation_agent.generate_scene_video()` | same | same |
| Provider API function | `muapi.submit_scene()` | same (or `hypogeum/byteplus_ark.py` for the non-MUAPI BytePlus Ark route, not read in this audit — out of scope, no Claude call there either per its own module purpose as a deterministic HTTP client) | `muapi.submit_scene()` |

**There is one universal final-prompt owner, not provider-specific owners**: VPD Stage B writes both `primary_prompt` and `fallback_prompt` for every scene in a single call, regardless of which tier/provider that scene is ultimately routed to. Provider-specific *vocabulary knowledge* is supplied to that one owner via two composed skills (`veo_translation_skill.compact.md`, `seedance_translation_skill.compact.md`) — the ownership is unified; only the *composed knowledge* is provider-specific.

---

## 3. Existing LLM Calls (Audit Q3)

**Six distinct system-prompt/call types exist in VPD's internal pipeline**, not one:

| # | Stage | System prompt constant | Writes prompt text? |
|---|---|---|---|
| 1 | A1 — metadata | `META_PROMPT` | No |
| 2 | A2 — scene skeleton | `SCENE_BATCH_PROMPT` | No |
| 3 | A4 — presentation planning | `PRESENTATION_PLANNER_PROMPT` | No |
| 4 | Shot Diversity Planner (separate module) | its own composed prompt | No — shot design constraints only |
| 5 | **B — cinematic prompt writing** | `SCENE_PROMPTS_PROMPT` | **Yes — the only one** |
| 6 | Visual Intent QA | `VISUAL_INTENT_QA_PROMPT` | No — scores existing prompts |

Stage B has two call *sites* (`VPD.py:747` primary batch, `VPD.py:1328` retry-with-smaller-batch on truncation) but both reuse the identical `SCENE_PROMPTS_PROMPT` constant — one call *type*, not two.

**Which call should receive each skill:** all three candidate skills (Seedance2, Veo Prompt Director, Veo Tools) are, by their own content, exclusively relevant to call type #5 (Stage B) — none of them concern metadata planning, scene skeletons, presentation method selection, shot diversity, or QA scoring.

**Direct attachment can preserve the existing call count for Seedance2 and Veo Prompt Director**, because Stage B's `_load_scene_prompts_prompt()` already composes four files into one `compose_system_prompt()` call (`vpd_scene_prompts.txt`, `cinematic_prompt_architecture.md`, `veo_translation_skill.compact.md`, `seedance_translation_skill.compact.md`) — extending or reconciling that same call adds zero new call sites and zero new call types, exactly like Humanizer's attachment to Script Engine and GPT-Image2's attachment to `thumbnail_prompt_writer.py`. **Veo Tools cannot be attached at all without adding a call** (see §5/§6) — its own workflow is not a system-prompt-composable artifact.

---

## 4. Skill Ownership Matrix

| Skill | Correct existing owning agent | Confirmed by |
|---|---|---|
| **Seedance2 Skill** | `hypogeum/agents/visual_package_director.py` — **Stage B only** (`SCENE_PROMPTS_PROMPT`) | `UPSTREAM_SKILL_MANIFEST.md` §3.3 "Exact intended target"; independently confirmed by this audit's own trace (§1–§2) — Stage B is the sole writer of `primary_prompt`/`fallback_prompt`, and Seedance2's content (Seedance prompt syntax) is relevant only there |
| **Veo Video Prompt Director** | `hypogeum/agents/visual_package_director.py` — **Stage B only** (`SCENE_PROMPTS_PROMPT`) | `UPSTREAM_SKILL_MANIFEST.md` §3.4 "Exact target"; same independent confirmation as Seedance2 |
| **Veo Tools** | **No existing owning agent** | `UPSTREAM_SKILL_MANIFEST.md` §3.5 "No current target agent — Confirmed — no agent in `hypogeum/agents/` is a candidate"; independently re-confirmed by this audit — no unattended, non-interactive, MUAPI-routed call site exists anywhere that could host a human-approval-gated CLI workflow |

**Not all three belong to the same agent in the same way** — Seedance2 and Veo Prompt Director share an owner (Stage B) because that is the sole video-prompt-writing call in the entire codebase, but Veo Tools has no owner at all because its content isn't prompt-craft knowledge composable into a system prompt — it's an interactive, credential-requiring, GCP-direct execution tool.

---

## 5. Skill Overlap and Conflict Matrix

| Pair | Classification | Basis |
|---|---|---|
| Seedance2 ↔ existing `seedance_translation_skill.md` | **Partially overlapping** — both describe Seedance prompt syntax, but Seedance2 targets a *different product identity* (Jimeng Seedance 2.0, ByteDance consumer app, 480–720p cap) than Hypogeum's actual production route (BytePlus Ark / MUAPI Seedance, documented 480p–4K). Wholesale replacement would **narrow**, not extend, documented capability. `UPSTREAM_SKILL_MANIFEST.md` §3.3 recommends *reconciliation* (extracting the one confirmed gap — `@Reference` role-assignment syntax) into the existing skill, not a fourth composed file. |
| Veo Prompt Director ↔ existing `veo_translation_skill.md` | **Partially overlapping, with one direct conflict** — `references/cinematography.md` recommends `Static` as an acceptable camera movement, directly contradicting `hypogeum/brand/video_style_system.md:345`'s prohibition on static camera for non-establishing shots (and `CLAUDE.md`'s own frozen rule: "no `static` camera verb — use `controlled slow push in`"). Per `UPSTREAM_SKILL_MANIFEST.md` §3.4's precedence policy, this specific entry must be omitted or scope-qualified, never presented to Stage B unqualified. |
| Seedance2 ↔ Veo Prompt Director | **Complementary** — disjoint provider vocabularies (Seedance syntax vs. Veo syntax), no direct conflict between the two upstream sources themselves. |
| Veo Tools ↔ everything else | **Conflicting at the architecture level, not the content level** — its workflow requires human approval before generation and calls GCP/Vertex AI directly, bypassing MUAPI, Provider Router, and Finance Gate entirely. This is not a prompt-content overlap question; it is a structural incompatibility with every unattended call site in the pipeline. |

**Would attaching multiple full skills to one system prompt duplicate cinematography guidance, create conflicting provider syntax, inflate context unnecessarily, or blur ownership?**

- **Duplicate cinematography guidance: yes, partially, already happening today at a smaller scale** — `cinematic_prompt_architecture.md` (the internal 9-layer spec) and the two provider translation skills already coexist in Stage B's composed prompt without documented conflict, because they operate at different layers (structural spec vs. vocabulary translation). Adding Veo Prompt Director's cinematography reference risks direct, not merely stylistic, duplication with `cinematic_prompt_architecture.md`, since both are general-purpose (not provider-specific) cinematography guidance — this is the strongest argument in this audit for *not* composing Veo Prompt Director wholesale.
- **Conflicting provider syntax: yes, confirmed** — the static-camera example above is a real, previously-identified conflict, not a hypothetical one.
- **Context inflation: yes, materially** — `veo_translation_skill` and `seedance_translation_skill` were each already reduced via a B1 compact-runtime pass (64,914→8,000 and 56,463→8,200 chars respectively) specifically because their full versions were too large for Stage B's budget. Composing additional full-length third-party content (Seedance2's `SKILL.md` at 15,337 B, Veo Prompt Director's 5 files at ~22 KB combined) without a similar reduction pass would reintroduce the exact problem B1 solved.
- **Blurred ownership between generic shot design and provider-specific formatting: not a new risk** — Stage B already cleanly separates "what to depict" (upstream of it: A1/A2/A4/Shot Diversity Planner) from "how to phrase it for a given provider" (Stage B itself, informed by the two translation skills). Adding more provider-specific vocabulary to Stage B does not blur this boundary; it is the boundary already in place.

---

## 6. Recommended Attachment Architecture

**Neither Seedance2 nor Veo Prompt Director fits the simple "compose the whole pinned `SKILL.md` verbatim" pattern used for Humanizer and GPT-Image2.** Both require **reconciliation** into the existing internal skills, not new files appended to the `compose_system_prompt()` call:

- **Seedance2**: MIT-licensed, git-hosted, redistributable — but its content describes a different Seedance product identity than Hypogeum's production route. The correct action (per `UPSTREAM_SKILL_MANIFEST.md` §3.3, independently re-confirmed here) is to **extract only the confirmed gap** (the `@Reference` role-assignment syntax) and merge it, explicitly attributed, into the *existing* `hypogeum/skills/seedance_translation_skill.md` — never a wholesale verbatim compose, and never an overwrite of the existing evidence-tagged content.
- **Veo Prompt Director**: license is **absent and unresolved** — per §3.4, this is a **binding legal condition**: the source's text must not be redistributed or extracted verbatim into any file until resolved. Only the *concepts* (emotion-to-action table, audio-direction syntax, multi-shot timestamp structure) may inform a Hypogeum-authored addition to the existing `veo_translation_skill.md`, never the source's literal prose. Direct `compose_system_prompt()` attachment of the raw file is **not legally available** regardless of architectural preference.
- **Veo Tools**: **not a system-prompt skill at all** — nothing to compose. Its value (if any, someday) is an entirely different integration shape (a human-approval-gated external tool), not a prompt-craft knowledge source. No conditional-loading question applies to it because there is no attachment point to condition.

**On the conditional-loading question (Q6):** neither Seedance2 nor Veo Prompt Director, once reconciled, changes today's existing unconditional-composition behavior — both `veo_translation_skill.compact.md` and `seedance_translation_skill.compact.md` are *already* composed into every Stage B call regardless of which scenes in that batch are Veo-tier vs. Seedance-tier routed, and Stage B's own prompt (`vpd_scene_prompts.txt`) relies on the model's judgment to apply the relevant provider's vocabulary per scene rather than a hard-coded branch. `UPSTREAM_SKILL_MANIFEST.md` §3.3 flags provider-conditional loading as a *desired future improvement* for Seedance2 specifically ("this content must load ... only when `provider_router.py` has assigned the scene to a Seedance-routed provider — not unconditionally alongside Veo content, as today's composition does") — this is a valid, separate, larger architectural change (would require restructuring Stage B's batching to be provider-homogeneous per call, or a templating mechanism inside the composed prompt) and is **explicitly out of scope for this task and not recommended as part of the reconciliation work**, which can proceed unconditionally exactly as today's two existing skills already do.

---

## 7. Installation Scope (Audit Q7, per skill)

| | Seedance2 | Veo Prompt Director | Veo Tools |
|---|---|---|---|
| Candidate pin | Commit `e06c7c63a766d623004a2807881c30685ce517af` (no tag; main HEAD as of 2026-02-18) | Local archive, SHA-256 `9e7f3600dc4eced97ccc17fcd4dc16e35ed4c2bfb3c2e7233137377991ae170e`, version `1.0.0`. **Note:** this task cites a different canonical URL (`https://mcpmarket.com/tools/skills/veo-video-prompt-director`) than the prior manifest's local-tarball source — this audit did not fetch that URL (no installation is authorized in this task, and the license-absence blocker below applies regardless of which copy is used), and flags the discrepancy for whoever performs the eventual reconciliation to resolve by confirming both sources are the same package before trusting the URL as a portable substitute for the checksummed local archive. | Tag `v1.0.0`, commit `f87f3a9e8531eb91f305722c5b2f5679318268d7` (reference-only pin; not for installation) |
| License | MIT | **Absent/unresolved — blocking** | MIT |
| Required SKILL.md/document files | `SKILL.md` (English, 15,337 B) — `zh/SKILL.md` explicitly excluded (no English-only pipeline consumer) | `SKILL.md` (4,047 B) + 4 reference files (~18 KB) — but see license blocker; concepts only, no verbatim extraction permitted | N/A — nothing installable under current constraints |
| Executable/CLI/provider files that must not be installed | None present in the skill surface itself (pure markdown skill) | None present | **All of it** — `skills/veo/scripts/veo-generate.ts`, `skills/veo-multi-shot/scripts/veo-multi-generate.ts`, `assemble-clips.sh`, `create-loop.sh` — every executable component, since the tool's entire value is the CLI/workflow this repository cannot host |
| Can the skill content stand alone without references/scripts? | Yes — `SKILL.md` alone is a complete document (unlike GPT-Image2, no confirmed "cannot function without a specific referenced file" dependency was found in the prior verification pass) | The 4 reference files carry most of the useful content (cinematography, prompt formula, audio directing, advanced workflows) — `SKILL.md` alone is a thinner 4 KB entry point; the reconciliation approach already recommended (concept-extraction, not whole-file installation) sidesteps this question | N/A |
| Interactive/output-format assumptions requiring a bridge | Not assessed in the prior verification pass at this granularity; likely minimal given MIT licensing and the reconciliation-not-compose approach means no bridge section is needed at all — a bridge is only required for whole-file `compose_system_prompt()` attachment, which this audit does not recommend for Seedance2 | Same reasoning — reconciliation avoids needing a bridge; the tool's own `references/` content was authored for a general prompt-crafting audience with no confirmed interactive-tool assumptions found in the prior pass | Its own workflow (`UNDERSTAND → CRAFT → VALIDATE → PRESENT & AWAIT APPROVAL → GENERATE → ITERATE`) is *entirely* interactive by design — this is the disqualifying property, not a bridgeable detail |

---

## 8. Schema, Pipeline, and Provider Impact

**None, for any of the three skills, under the recommended architecture.** Reconciling Seedance2/Veo Prompt Director content into the *existing* `seedance_translation_skill.md`/`veo_translation_skill.md` files changes only the *content* of two already-wired internal skills — it touches no schema (`visual_package`'s shape is unaffected; `primary_prompt`/`fallback_prompt` remain plain strings), no pipeline stage (Stage B already exists at its current position), and no provider routing (`provider_router.py`, `muapi.py`, and tier assignment are all untouched, confirmed by this audit's own reads showing zero LLM-call surface in either file). Veo Tools requires no schema/pipeline/provider consideration because it is not being attached at all.

---

## 9. No New Stage or Agent Required — Explicit Statement

**This audit found no evidence requiring a new agent, pipeline stage, adapter, checkpoint, or schema for Seedance2 or Veo Prompt Director.** The owning agent (`visual_package_director.py`, specifically Stage B) already exists, is already reachable through the current pipeline (`pipeline.py` → `_run_visual_package_director()` → `run_visual_package_director()`, unmodified), already makes exactly the LLM call type (`SCENE_PROMPTS_PROMPT` via `run_agent()`) that would receive this content, and already demonstrates the exact composition mechanism (`compose_system_prompt()` with multiple skill files) needed. Veo Tools requires no new agent either — it requires no attachment of any kind, per §4/§6.

---

## 10. Confirmed Defects and Non-Defect Observations

No new defects were found in the current VPD/Stage-B/provider-routing architecture during this audit. One pre-existing, already-documented item is worth restating for completeness: the unconditional (not provider-tier-conditional) composition of both translation skills into every Stage B batch was already identified as a design characteristic (not a defect — §6) by the original manifest pass, and remains unchanged and un-worsened by this audit's findings.

**Non-defect observations:**
- N1. VPD Stage B is architecturally identical in kind to Script Engine and `thumbnail_prompt_writer.py` — a single `run_agent()` call whose system prompt is composed via `compose_system_prompt()` from a base prompt plus multiple skill files — confirming the "skills attach directly to the existing owning agent's single call" pattern generalizes cleanly to this third case, even though the *specific* attachment mechanics differ (reconciliation vs. whole-file compose) due to licensing and content-identity constraints unique to these two sources.
- N2. `shot_diversity_planner.py` is a legitimate, separate `run_agent()` call site with its own skill composition (`visual_language_engine.compact.md`) — correctly out of scope for all three candidate skills, since none of their content concerns shot-diversity assignment.
- N3. The prior manifest pass's conclusions (§3.3–§3.5) were re-verified against the current codebase and found to still hold with zero drift — `visual_package_director.py`'s Stage B remains the correct, sole target; no code change since that pass altered this.

---

## 11. Exact Smallest Next Implementation Step

**Not implemented by this audit.** Three independent next steps exist, one per skill, none of which should be bundled into a single commit:

1. **Seedance2 reconciliation** — the smallest of the three: extract the confirmed `@Reference` role-assignment syntax gap and add it, explicitly attributed, as a new subsection of `hypogeum/skills/seedance_translation_skill.md` (and regenerate its compact runtime variant per the existing B1 compaction convention). No new file under `third_party/skills/`, no `compose_system_prompt()` signature change — the existing composition call already includes this file.
2. **Veo Prompt Director reconciliation** — blocked until the license question is resolved (who distributed the archive, under what terms) — this must happen before any concept-extraction work, let alone verbatim use, per §7's binding condition. The next action is a licensing inquiry, not an engineering task.
3. **Veo Tools** — no next step recommended. Remains reference-only per the existing manifest state unless a future task separately proposes and approves the full prerequisite list already named in `UPSTREAM_SKILL_MANIFEST.md` §3.5 (Identity/Secret Management, Finance Gate, Provider Router, cost tracking, retry/failure policy, observability, autonomous runtime, provider comparison, configuration/deployment portability) — a scope far larger than a skill-attachment task.

---

## 12. Final Decision

**SPLIT BY EXISTING PROVIDER-SPECIFIC OWNERS — with a qualification the label doesn't fully capture: all three skills route to the same single owning agent and call site (Stage B), but each requires a different disposition.**

None of the four fixed decision options maps perfectly onto three skills with three different outcomes, so this audit states the split explicitly rather than force-fitting one label to all three:

- **Seedance2 → INSTALL FIRST, THEN ATTACH** (attach means *reconcile into the existing internal skill*, not compose a new file) — MIT-licensed, git-hosted, no blocking constraint, smallest of the three.
- **Veo Prompt Director → INSTALL FIRST, THEN ATTACH, BLOCKED ON LICENSE** — same target and mechanism as Seedance2, but installation itself cannot proceed (not even for concept-extraction purposes beyond what's already recorded in the manifest) until the license is resolved.
- **Veo Tools → DO NOT ATTACH — REDUNDANT** is not quite accurate either (its content isn't redundant with anything; it's simply inapplicable) — the precise finding is **no compatible owner exists and none is proposed**, per §4/§6, closest to "DO NOT ATTACH" among the given options.

**No skill in this batch is ready for the "INSTALL AND ATTACH TO EXISTING OWNER" pattern used for Humanizer and GPT-Image2** — both Seedance2 and Veo Prompt Director require reconciliation into existing content rather than net-new composition, and Veo Prompt Director additionally requires a licensing resolution before any installation step at all.
