# Visual Language Engine — Compact Runtime Reference
<!-- SOURCE: hypogeum/skills/visual_intelligence/visual_language_engine.md v1.0 | COMPACT: B1 -->
<!-- PURPOSE: Runtime-only condensed version. Full skill retained for audit/reference. -->

## Core Principle

**Every presentation decision must be explicit before a prompt is written.**

Presentation method is determined in Stage A4 and is an immutable constraint for Stage B. Stage B MUST NOT decide HOW to present a scene as a side-effect of writing the cinematic prompt.

**FORBIDDEN DEFAULTS (never use unless explicitly planned):**
- `split_screen_for_two_ideas` ← most common error
- `side_by_side_composition`
- `independent_camera_moves_composited`
- `talking_head`
- `news_package_style`
- `static_camera`

## Narrative Intent → Presentation Method Mapping

| Narrative Intent | Candidate Methods |
|---|---|
| hidden_structure | architectural_cutaway, transparent_floor, xray_reveal |
| scale | human_reference, wide_establishing |
| comparison | match_transition, before_after, morph |
| mechanism | exploded_view, mechanical_cross_section |
| underground | section_cut, vertical_reveal |
| discovery | slow_reveal, push_in, object_reveal |
| passage_of_time | time_lapse_environment, erosion_reveal, layered_stratigraphy |
| power_and_scale | low_angle_hero, overhead_dominance, wide_crowd |
| mystery | partial_reveal, shadow_obscure, approach_reveal |
| human_experience | point_of_view, close_up_reaction, texture_intimacy |

## Presentation Methods — Key Rules

| Method | Camera Strategy | Spatial Rule | FORBIDDEN |
|---|---|---|---|
| architectural_cutaway | Single continuous dolly/push through cutaway plane | Upper and lower worlds in same frame | split_screen, side_by_side, independent_camera_moves |
| transparent_floor | Top-down or high angle with downward tilt | Transparency is physical — audience sees through naturally | split_screen, reaction_shot_cutaway |
| xray_reveal | Slow push in while material becomes progressively transparent | Inside and outside coexist in one frame | hard_cut_to_interior, split_screen |
| section_cut | Slow vertical tilt or pull-back from cut face | Depth and layers readable in single frame | split_screen, alternating_cuts |
| vertical_reveal | Continuous vertical descent or ascent | Each level transitions without cuts | cut_between_levels, split_screen |
| slow_reveal | Controlled slow push in or pull back | Hidden and revealed share same frame | immediate_full_reveal, hard_cut_to_reveal |
| push_in | Controlled slow push in — never zoom | Subject remains centered; background compresses naturally | cut_to_close_up, zoom_distortion |
| human_reference | Wide or medium shot placing human at edge of frame | Human and subject share same frame simultaneously | abstract_size_text, disconnected_scale_shot |
| exploded_view | Slow rotation or push-through the exploded arrangement | All components visible in single frame with spatial gaps | separate_shots_per_component, diagram_overlay |
| mechanical_cross_section | Slow pan across exposed cross-section face | External housing and internal mechanism coexist in frame | external_shot_only, split_screen_interior_exterior |
| point_of_view | Handheld drift from character height | Viewer sees what character sees — not the character | third_person_observation |
| low_angle_hero | Slow tilt up from ground level | Sky or ceiling visible above subject | eye_level_for_power_shots |
| overhead_dominance | Overhead descent | Subject fills frame from above; edges imply infinite continuation | side_angle_for_overhead_concepts |

## Spatial Relationship Templates

Use for the `spatial_relationship` field in the presentation plan:

- **above_below**: "The {above_element} exists directly above the {below_element} — both visible in one continuous frame"
- **inside_outside**: "The interior structure is revealed within its exterior housing — no cut required"
- **before_after**: "Past and present states share the same physical location — transition is temporal, not spatial"
- **micro_macro**: "The close detail exists within its larger context — scale readable without cutting away"

## Hard Constraints for Stage B

When writing `primary_prompt`, Stage B MUST:

1. Satisfy the `presentation_method` from the scene's `presentation_plan`.
2. Include the `camera_strategy` verbatim or paraphrase it accurately.
3. Describe the `spatial_relationship` — physical coexistence of elements in one frame.
4. Never introduce any technique listed in `forbidden_techniques`.
5. Never default to split_screen simply because two ideas need to be shown.

If `presentation_method: "architectural_cutaway"` → prompt MUST describe a single integrated frame with cutaway cross-section. NOT two separate camera feeds, NOT two independent shots, NOT split-screen.

## Rejection Criteria (Visual Intent QA)

A prompt FAILS QA if any of the following are true:

- Describes two independent camera movements for a scene with a single-frame presentation method.
- The `spatial_relationship` from the presentation plan is absent from the prompt.
- The prompt introduces a `forbidden_technique` from the plan's `forbidden_techniques` list.
- An underground scene uses neither `section_cut` nor `vertical_reveal`.
- A reveal scene uses `immediate_full_reveal` or describes the subject appearing instantly.
- The `presentation_method` from the plan is not recognisable in the written prompt.
