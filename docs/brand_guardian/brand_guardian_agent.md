# Brand Guardian Agent (BGA)

**Status:** Architecture defined. Not yet implemented.  
**Implementation milestone:** M6  
**Depends on:** `brand_bible_hypogeum.json` content populated  
**Does not block:** Episode Zero (runs without BGA until Bible is populated)

---

## Mission

The Brand Guardian Agent is the single authority for visual identity consistency across the entire Hypogeum production factory.

Its purpose is to ensure that the brand identity of the channel remains constant as providers change, models are upgraded, agents are modified, and the factory scales. The provider is an implementation detail. The identity is not.

No agent other than the BGA is permitted to interpret, define, or override brand rules. All agents that produce visual output consume brand guidance generated or validated by the BGA. They do not carry their own copy of brand rules.

---

## Authority Boundaries

**BGA owns exclusively:**
- Color system definitions and enforcement
- Typography system definitions and enforcement
- Thumbnail visual language and composition rules
- Motion language rules (what types of motion are on-register)
- Composition language (framing conventions, subject placement, depth)
- Documentary visual grammar (the permanent rules governing scene construction)
- Logo usage rules
- Iconography rules
- Visual hierarchy standards
- Atmosphere rules (tone, mood registers that are on/off brand)
- Forbidden visual patterns (exhaustive list of what is permanently prohibited)
- Social media and Shorts visual rules

**BGA does not own:**
- Historical accuracy decisions (owned by Research Director and Script Engine)
- Narrative structure decisions (owned by Narrative Architect)
- Provider routing decisions (owned by VPD and the provider architecture)
- Scene timing and duration decisions (owned by VPD)
- Caption copy (owned by Script Engine and Audio Agent)
- Music selection (owned by Audio Agent)
- Platform publishing configuration (owned by Publishing Agent)

**The BGA is not a creative agent.** It does not generate prompts, write copy, or design visuals. It validates that the output of other agents conforms to the brand system defined in `brand_bible_hypogeum.json`. All creative decisions belong to the producing agent; brand compliance is post-hoc validation.

---

## Responsibilities

1. Load `brand_bible_hypogeum.json` at the start of each pass.
2. Validate the producing agent's output against every applicable Bible section.
3. Produce a structured compliance report for each pass: `bga_compliance_report_{pass}_{episode_id}.json`.
4. Emit one of three outcomes per pass: `APPROVED`, `APPROVED_WITH_FLAGS`, or `REJECTED`.
5. On `REJECTED`: provide specific, actionable violation entries that the producing agent can act on.
6. On `APPROVED_WITH_FLAGS`: allow the pipeline to continue; flag violations are logged and surfaced at publishing review.
7. Never generate, modify, or re-write the output of another agent. The BGA validates only.
8. Never add creative content to a producing agent's output. If a scene prompt is non-compliant, the BGA flags it — it does not rewrite it.

---

## Pipeline Position

```
EP
→ RD
→ NA
→ SE
→ VPD  [Claude planning phase only — produces visual_package with generation_status=planned]
→ BGA Pass 1  [validate visual plan before MUAPI execution]
→ VPD  [MUAPI execution phase — submits scenes to provider]
→ TD   [Claude planning phase only — produces thumbnail_generation_plan]
→ BGA Pass 2  [validate thumbnail plan before image generation]
→ TD   [Image generation phase]
→ Audio
→ Assembly
→ BGA Pass 3  [validate final render package before publishing]
→ Publishing
```

**Key architectural principle:** BGA intervenes before generation cost is incurred where possible.
- Pass 1 runs after planning, before any MUAPI credits are spent.
- Pass 2 runs after thumbnail planning, before any image generation credits are spent.
- Pass 3 runs after assembly, before any publishing action is taken.

---

## Pass 1 — Visual Plan Validation

**Trigger:** After VPD Phase 1 (Claude planning) completes and before VPD Phase 2 (MUAPI execution) begins.

**Input:** `visual_package` with `generation_status=planned` for all scenes.

**Scope of validation:**
- Every scene's `production_spec.primary_prompt` and `fallback_prompt` are checked against forbidden visual patterns.
- Every scene's `visual_spec.motion_type` is checked against the motion language rules.
- Every scene's `visual_spec.atmosphere` and `mood` are checked against atmosphere rules.
- Every scene's `visual_spec.framing` is checked against composition language rules.
- Period palette references (`color_treatment`) are validated against the color system.
- The documentary visual grammar rules are applied to every scene that contains a human figure (checked via subject field).
- The `locked_entities` continuity record is checked for visual spec compliance with brand identity conventions.
- The `thumbnail_handoff_brief` is pre-validated (not final validation — that happens in Pass 2).

**Rejection criteria (Pass 1):**
- Any scene prompt explicitly references a forbidden visual pattern.
- More than 20% of scenes have motion type violations.
- A final-locked entity's visual spec contains a forbidden element that cannot be corrected without entity unlock.
- The thumbnail brief's `negative_constraints[]` are missing required brand-level prohibitions.

**Approval criteria (Pass 1):**
- All scene prompts are free of forbidden patterns.
- All motion types are on-register per the motion language system.
- All atmosphere/mood values are within the defined register.
- Entity visual specs are compliant.

**Flag criteria (proceed with flags):**
- Isolated non-compliant scenes (≤ 20% of total) where the violation is low-severity (atmosphere, framing).
- Missing optional brand elements (e.g., colour treatment not specified) where defaults apply.

**Output:** `bga_compliance_report_pass1_{episode_id}.json`

---

## Pass 2 — Thumbnail Plan Validation

**Trigger:** After Thumbnail Director Phase 1 (Claude planning) and before image generation.

**Input:** `thumbnail_generation_plan` produced by TD.

**Scope of validation:**
- All thumbnail candidates are checked against the thumbnail language rules.
- All thumbnail candidates are checked against forbidden patterns.
- Brand-level `negative_constraints` from the Bible are enforced (in addition to episode-specific constraints from VPD).
- Typography and text overlay decisions in thumbnail candidates are checked against the typography system.
- Composition of each candidate is checked against visual hierarchy standards.
- Logo placement rules are checked if a logo or channel mark is specified.
- The `primary_subject_identity` binding is verified — subject must be on-register per brand iconography rules.

**Rejection criteria (Pass 2):**
- Any candidate references a forbidden pattern.
- The primary subject treatment violates a non-negotiable brand rule.
- Typography decisions violate binding typographic constraints.

**Approval criteria (Pass 2):**
- All candidates are brand-compliant.
- Typography decisions are within the defined system.
- Visual hierarchy is correctly applied.

**Flag criteria (proceed with flags):**
- A candidate uses a non-preferred but permissible composition style.
- Color treatment diverges from brand palette but remains within acceptable range.

**Output:** `bga_compliance_report_pass2_{episode_id}.json`

---

## Pass 3 — Final Render Validation

**Trigger:** After Assembly Agent completes and before Publishing Agent runs.

**Input:** `final_render_package` with all render paths populated.

**Scope of validation:**
- Verify that all Pass 1 and Pass 2 flag items have been resolved or explicitly accepted.
- Verify that the episode render manifest does not include any assets that were flagged as non-compliant and never resolved.
- Verify that the publishing metadata (title, description, tags) does not contain language that conflicts with brand positioning rules.
- Verify that the AI disclosure is present and formatted per brand publishing rules.
- Verify that Shorts metadata (if present) conforms to Shorts-specific brand rules.
- Verify that logo or channel mark assets are present where required.

**Rejection criteria (Pass 3):**
- Any unresolved Pass 1 or Pass 2 REJECTED item is still present in the render package.
- AI disclosure is absent or malformed.
- Publishing metadata contains brand-prohibited language.

**Approval criteria (Pass 3):**
- All prior compliance flags are resolved or accepted.
- Metadata is brand-compliant.
- Disclosure is correct.

**Output:** `bga_compliance_report_pass3_{episode_id}.json`

---

## Compliance Report Schema

Each pass produces a compliance report:

```json
{
  "report_id": "<string>",
  "pass": 1 | 2 | 3,
  "episode_id": "<string>",
  "bible_version": "<string>",
  "produced_at": "<ISO 8601>",
  "outcome": "APPROVED" | "APPROVED_WITH_FLAGS" | "REJECTED",
  "violations": [
    {
      "violation_id": "<string>",
      "severity": "BLOCKING" | "FLAG",
      "rule_reference": "<bible section + rule ID>",
      "location": "<scene_id | candidate_id | field path>",
      "description": "<what the violation is>",
      "remediation": "<what the producing agent must change>",
      "resolved": false
    }
  ],
  "summary": {
    "blocking_count": 0,
    "flag_count": 0,
    "scenes_checked": 0,
    "scenes_compliant": 0
  }
}
```

---

## Agent Model

**Model:** Claude Sonnet 4.6  
**Max tokens:** 4,096 per pass (compliance reports are structured and compact)  
**System prompt location:** `hypogeum/prompts/brand_guardian_agent.txt`  
**Bible location:** `brand_bible_hypogeum.json` (project root)  
**Output schema:** `bga_compliance_report` (validated by `schemas.py`)

---

## Implementation Notes

- The BGA is a pure validation agent. It makes no MUAPI calls, no ElevenLabs calls, and no platform API calls.
- The BGA is stateless between episodes. It reads the Bible fresh on each invocation.
- If `brand_bible_hypogeum.json` does not exist or has `_status: "TBD"` sections, the BGA skips those sections and logs `bible_section_unpopulated` warnings. It does not block.
- During the Episode Zero phase and until the Bible is populated, all three BGA passes are ADVISORY ONLY — they emit compliance reports but never REJECT.
- Once the Bible is populated and the `_global_status` field is set to `"active"`, the BGA enforcement mode activates.
