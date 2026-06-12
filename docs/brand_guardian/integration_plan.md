# Brand Bible Integration Plan

**Status:** Architecture defined. Not yet implemented.  
**Prerequisite:** `brand_bible_hypogeum.json` populated from approved Version 2 visual reference.  
**BGA enforcement mode activates when:** `brand_bible_hypogeum.json._meta._global_status = "active"`

---

## Which Agents Consume the Bible

| Agent | Pass | Sections Consumed | When |
|---|---|---|---|
| Visual Package Director | Pass 1 | `documentary_visual_grammar`, `motion_language`, `color_system`, `forbidden_patterns`, `iconography` | After VPD planning phase, before MUAPI execution |
| Thumbnail Director | Pass 2 | `thumbnail_language`, `color_system`, `typography`, `forbidden_patterns`, `logo_system` | After TD planning phase, before image generation |
| Assembly Agent | Pass 3 (indirect) | `typography`, `shorts_rules`, `forbidden_patterns` | Assembly reads Bible directly for text overlay and Shorts rules; BGA validates the output |
| Publishing Agent | Pass 3 | `publishing_rules`, `social_media_rules`, `forbidden_patterns` | BGA validates before any platform API call is made |
| Script Engine | None (read-only advisory) | `identity`, `publishing_rules.title_rules` | SE may consult Bible for tone/register alignment; BGA does not validate SE output |
| Narrative Architect | None | — | NA does not consume visual rules |
| Audio Agent | None | — | Audio Agent does not consume visual rules |
| Executive Producer | None | `identity` (advisory) | EP may consult identity section for topic-to-brand fit; not validated by BGA |

---

## VPD — Visual Package Director

**Consumes:** Pass 1 BGA compliance report  
**When:** The BGA runs between VPD Phase 1 (Claude planning) and VPD Phase 2 (MUAPI execution). VPD Phase 2 does not begin until BGA Pass 1 returns `APPROVED` or `APPROVED_WITH_FLAGS`.

**What VPD is allowed to override:**
- `color_system.thumbnail_palette` — VPD may select period-specific palettes that diverge from the brand palette when historical accuracy requires it. Must document in `visual_integrity_log`.
- `motion_language.preferred_motion_by_scene_type` — VPD may use a non-preferred motion type when narrative signal requirements override it. Must document in `visual_integrity_log`.
- `iconography.preferred_symbolic_vocabulary` — Period-accuracy takes precedence over brand iconographic preferences. VPD may use period-accurate symbols not on the preferred list.

**What VPD is forbidden to override:**
- `documentary_visual_grammar.forbidden_visual_modes` — Absolute. No exception. No prompt, config, or narrative signal can authorize a talking head, interview framing, or visible lip sync.
- `forbidden_patterns.visual_patterns` — All items on this list are permanent and non-negotiable.
- `forbidden_patterns.content_patterns` — Historical integrity rules are absolute. No fabricated footage, no synthesized voice.
- `color_system.period_palette_constraints.global_forbidden_colors` — Even period-accurate palettes may not use globally forbidden colors.

**On BGA Pass 1 REJECTED:**  
VPD Phase 2 does not execute. Claude re-plans the affected scenes only (targeted re-run, not full episode re-plan). BGA re-validates the revised scenes. Max two revision cycles before the episode is escalated to human review.

---

## TD — Thumbnail Director

**Consumes:** Pass 2 BGA compliance report  
**When:** The BGA runs between TD Phase 1 (Claude planning of `thumbnail_generation_plan`) and TD Phase 2 (image generation). TD Phase 2 does not begin until BGA Pass 2 returns `APPROVED` or `APPROVED_WITH_FLAGS`.

**What TD is allowed to override:**
- `color_system.thumbnail_palette.allowed_range` — TD may use colors outside the brand palette when CTR evidence (A/B test data or TD's judgment) supports it. Must document in `thumbnail_integrity_log`.
- `thumbnail_language.subject_placement` — TD may deviate from preferred subject placement for a specific candidate if a higher CTR composition is justified. Must document.
- `thumbnail_language.approval_threshold` — TD may select a candidate below the recommended strength score if all higher-scoring candidates were rejected by BGA or by historical integrity constraints.

**What TD is forbidden to override:**
- `forbidden_patterns` — Any item on the forbidden list applies to all thumbnail candidates without exception.
- `thumbnail_language.face_rules` — When populated, these rules govern whether and how human faces may appear in thumbnails. Until populated, current episode-level `negative_constraints` from VPD apply.
- `typography.forbidden_typefaces` — No thumbnail may use a typeface outside the defined system.
- `typography.thumbnail_type.max_words` — Hard character/word limits on thumbnail text are not negotiable.
- `identity.visual_personality` — Thumbnails must reflect the channel's defined visual personality. A thumbnail that contradicts the brand character (e.g., sensationalist treatment on a credibility-positioned channel) is a binding violation.

**On BGA Pass 2 REJECTED:**  
TD revises the non-compliant candidates only. If all candidates in a pool are rejected, TD generates a new pool. Max two revision cycles per pool before PDR to EP.

---

## Assembly Agent

**Consumes:** `brand_bible_hypogeum.json` directly (not via BGA)  
**When:** At runtime, before rendering text overlays and Shorts.  
**Validated by:** BGA Pass 3 after assembly completes.

**What Assembly is allowed to override:**
- `typography.shorts_overlay_type.safe_zone_margin` — Assembly may adjust safe zone margin for a specific scene if VPD's `text_safe_zone` value requires it.
- `shorts_rules.overlay_structure.overlay_position_options` — Assembly places overlays based on VPD's `text_safe_zone` field. If VPD specifies `bottom`, Assembly uses `bottom` even if the Bible's preferred position is `top_third`.

**What Assembly is forbidden to override:**
- `typography.forbidden_typefaces` — Text rendered in video frames must use only permitted typefaces.
- `shorts_rules.overlay_structure.required_beats` — All three required text overlay beats (hook, key information, payoff) must be present in every Short.
- `shorts_rules.max_words_per_overlay` — Hard limit. No overlay may exceed this.
- `shorts_rules.redirect_text_rule` — Shorts redirect overlays must comply with the rule exactly.
- `forbidden_patterns` — Assembly may not add any visual element that appears on the forbidden patterns list, regardless of source.

**On BGA Pass 3 violation in Assembly output:**  
If Assembly introduced a non-compliant element (e.g., wrong typeface, forbidden overlay pattern), the Assembly Agent re-renders the affected segment only. Full episode re-assembly is not required unless the violation is in the main episode master.

---

## Publishing Agent

**Consumes:** Pass 3 BGA compliance report  
**When:** Publishing Agent does not execute until BGA Pass 3 returns `APPROVED` or `APPROVED_WITH_FLAGS`. On `REJECTED`, no platform API calls are made.

**What Publishing is allowed to override:**
- `publishing_rules.description_rules` — Publishing Agent may reorder optional description sections for platform-specific character limits.
- `social_media_rules.*.title_card_rules` — Platform-specific title card formatting may be adjusted within the permitted character limits.

**What Publishing is forbidden to override:**
- `publishing_rules.ai_disclosure.required` — AI disclosure must appear in every published piece without exception.
- `publishing_rules.ai_disclosure.forbidden_to_omit` — Even if the disclosure increases description length beyond optimal, it cannot be removed.
- `publishing_rules.description_rules.required_sections` — All required sections (logline, chapters, sources, ai_disclosure) must be present.
- `forbidden_patterns.content_patterns` — No publishing metadata may describe the content in a way that contradicts the historical integrity rules.

**On BGA Pass 3 REJECTED:**  
Publishing Agent halts entirely. No platform receives any content. The violation is escalated to human review. This is the only pass where a REJECTED outcome requires human resolution — there is no automated remediation path for a publishing-level brand violation.

---

## Bible Section Override Summary

| Section | VPD | TD | Assembly | Publishing |
|---|---|---|---|---|
| `identity` | Read-only | Read-only | Read-only | Read-only |
| `color_system.primary_palette` | Not consumed | Not overridable | Not consumed | Not consumed |
| `color_system.thumbnail_palette` | Period override permitted | CTR override permitted | Not consumed | Not consumed |
| `color_system.global_forbidden_colors` | **Never override** | **Never override** | Not consumed | Not consumed |
| `typography` | Not consumed | **Never override typefaces** | Margin adjustment permitted | Not consumed |
| `thumbnail_language` | Not consumed | Composition override permitted | Not consumed | Not consumed |
| `documentary_visual_grammar.forbidden_visual_modes` | **Never override** | Not consumed | Not consumed | Not consumed |
| `motion_language.on_register` | Non-preferred permitted with doc | Not consumed | Not consumed | Not consumed |
| `motion_language.forbidden_motion_patterns` | **Never override** | Not consumed | Not consumed | Not consumed |
| `logo_system` | Not consumed | Placement within rules | Not consumed | Not consumed |
| `forbidden_patterns` (all) | **Never override** | **Never override** | **Never override** | **Never override** |
| `shorts_rules.overlay_structure` | Not consumed | Not consumed | Position follows VPD | Not consumed |
| `shorts_rules.max_words_per_overlay` | Not consumed | Not consumed | **Never override** | Not consumed |
| `publishing_rules.ai_disclosure` | Not consumed | Not consumed | Not consumed | **Never override** |
| `social_media_rules` | Not consumed | Not consumed | Not consumed | Format adjustment permitted |

**Legend:** "Never override" = binding rule, pipeline must halt rather than proceed with a violation. "Not consumed" = this agent has no read access to this section. "Not overridable" = agent reads section but may not deviate.

---

## Transition State (Current — Pre-Bible)

Until `brand_bible_hypogeum.json._meta._global_status` is set to `"active"`:

1. **BGA passes are advisory only.** All three passes run and produce compliance reports, but REJECTED outcomes are downgraded to APPROVED_WITH_FLAGS. The pipeline never halts.
2. **Documentary visual grammar rules are already enforced** directly in the VPD system prompt — independent of the BGA. These rules are active now. The BGA formalises enforcement but does not create new rules.
3. **Episode Zero proceeds without BGA.** VPD and TD operate under their existing system prompts. BGA is not in the pipeline yet.
4. **The Bible is populated in parallel** with ongoing production. The first episode does not wait for the Bible.

---

## Activation Checklist

Before setting `_global_status: "active"`:

- [ ] All `_status: "TBD"` sections in the Bible are populated from approved Version 2 visual reference
- [ ] `forbidden_patterns` list is reviewed and finalised
- [ ] `documentary_visual_grammar` section reviewed (currently pre-populated from existing rules)
- [ ] `publishing_rules.ai_disclosure.required_text` is set to the exact approved disclosure text
- [ ] `shorts_rules._generation_model_note` is updated to reflect the active Shorts model (extracted/disabled)
- [ ] BGA agent system prompt (`hypogeum/prompts/brand_guardian_agent.txt`) is written and tested
- [ ] BGA schema added to `hypogeum/schemas.py` (`bga_compliance_report`)
- [ ] BGA integrated into `hypogeum/agents/visual_package_director.py` (between Phase 1 and Phase 2)
- [ ] BGA integrated into `hypogeum/agents/thumbnail_director.py` (between Phase 1 and Phase 2)
- [ ] BGA integrated into `hypogeum/pipeline.py` (Pass 3 before publishing)
- [ ] At least one full episode run with BGA in advisory mode before setting to active
