# Visual Language Engine — Reference Document

**Version**: 1.0
**Owner**: visual_package_director
**Used by**: Stage A4 (Scene Presentation Planner), Stage B (Cinematic Prompt Writer), Editorial QA

This document describes the Visual Language Engine pattern library. It is injected into LLM system prompts at runtime. Every planning and prompt-writing stage must comply with these patterns.

---

## The Core Principle

**Every presentation decision must be explicit before a prompt is written.**

Claude must NOT decide HOW to present a scene as a side-effect of writing the cinematic prompt. The presentation method — architectural_cutaway, slow_reveal, point_of_view, etc. — is determined in Stage A4 and becomes an immutable constraint for Stage B.

The forbidden defaults are NEVER the answer to "what is the best way to show two ideas?":

```
FORBIDDEN DEFAULTS (never use unless explicitly planned):
- split_screen_for_two_ideas       ← most common error
- side_by_side_composition
- independent_camera_moves_composited
- talking_head
- news_package_style
- static_camera
```

---

## Narrative Intent → Presentation Method Mapping

Each scene has a **narrative goal** — what the frame is doing in the story. That goal maps to candidate presentation methods.

| Narrative Intent     | Candidate Methods                                              |
|----------------------|----------------------------------------------------------------|
| hidden_structure     | architectural_cutaway, transparent_floor, xray_reveal         |
| scale                | human_reference, wide_establishing                            |
| comparison           | match_transition, before_after, morph                         |
| mechanism            | exploded_view, mechanical_cross_section                       |
| underground          | section_cut, vertical_reveal                                  |
| discovery            | slow_reveal, push_in, object_reveal                           |
| passage_of_time      | time_lapse_environment, erosion_reveal, layered_stratigraphy  |
| power_and_scale      | low_angle_hero, overhead_dominance, wide_crowd                |
| mystery              | partial_reveal, shadow_obscure, approach_reveal               |
| human_experience     | point_of_view, close_up_reaction, texture_intimacy            |

---

## Presentation Methods — Full Reference

### architectural_cutaway
**Description**: Single integrated frame showing both exterior and interior simultaneously through a structural cross-section.
**When to use**: When revealing hidden structures, underground spaces, or internal mechanisms.
**Camera strategy**: Single continuous dolly or slow push through the cutaway plane.
**Spatial rule**: Upper and lower worlds must exist in the same frame.
**FORBIDDEN**: split_screen, side_by_side, independent_camera_moves, composited_separate_videos.

### transparent_floor
**Description**: Camera positioned above a transparent surface revealing what lies beneath.
**When to use**: Underground reveals, basement structures, aquatic floor revealing seabed.
**Camera strategy**: Top-down or high angle with downward tilt.
**Spatial rule**: Transparency is physical — audience sees through the floor naturally.
**FORBIDDEN**: split_screen, reaction_shot_cutaway.

### xray_reveal
**Description**: Gradual transparency effect revealing internal structure within a single continuous shot.
**When to use**: Biological structures, building interiors, mechanical internals.
**Camera strategy**: Slow push in while material becomes progressively transparent.
**Spatial rule**: Inside and outside coexist in one frame during the reveal.
**FORBIDDEN**: hard_cut_to_interior, split_screen.

### section_cut
**Description**: Architectural or geological cross-section showing vertical stratification.
**When to use**: Underground spaces, geological layers, multi-level structures.
**Camera strategy**: Slow vertical tilt or pull-back from cut face.
**Spatial rule**: Depth and layers readable in single frame.
**FORBIDDEN**: split_screen, alternating_cuts.

### vertical_reveal
**Description**: Camera descends or ascends to reveal successive levels of a vertical space.
**When to use**: Multi-story underground spaces, cliff faces, tall structures.
**Camera strategy**: Continuous vertical descent or ascent.
**Spatial rule**: Each level transitions to the next without cuts.
**FORBIDDEN**: cut_between_levels, split_screen.

### slow_reveal
**Description**: Object or space emerges from darkness, obstruction, or partial concealment in a single continuous shot.
**When to use**: Discovery moments, first reveals, hidden objects.
**Camera strategy**: Controlled slow push in or pull back to reveal.
**Spatial rule**: What is hidden and what is revealed share the same frame.
**FORBIDDEN**: immediate_full_reveal, hard_cut_to_reveal.

### push_in
**Description**: Camera approaches subject with progressive magnification and intimacy.
**When to use**: Focusing attention on a specific detail, building tension toward a revelation.
**Camera strategy**: Controlled slow push in — never zoom.
**Spatial rule**: Subject remains centered; background compresses naturally.
**FORBIDDEN**: cut_to_close_up, zoom_distortion.

### human_reference
**Description**: Human figure or body part included to establish scale relationship.
**When to use**: Establishing the size of massive structures, tiny artifacts, geological features.
**Camera strategy**: Wide or medium shot placing human at edge of frame.
**Spatial rule**: Human and subject share the same frame simultaneously.
**FORBIDDEN**: abstract_size_text, disconnected_scale_shot.

### exploded_view
**Description**: Components of a mechanism separated and arranged in space to show assembly relationships.
**When to use**: Complex mechanisms, gears, architectural joints, multi-part artifacts.
**Camera strategy**: Slow rotation or push-through the exploded arrangement.
**Spatial rule**: All components visible in single frame with spatial gaps.
**FORBIDDEN**: separate_shots_per_component, diagram_overlay.

### mechanical_cross_section
**Description**: Internal working parts of a mechanism revealed through a physical cut plane.
**When to use**: Clockwork, gears, hydraulics, internal mechanisms.
**Camera strategy**: Slow pan across the exposed cross-section face.
**Spatial rule**: External housing and internal mechanism coexist in frame.
**FORBIDDEN**: external_shot_only, split_screen_interior_exterior.

### point_of_view
**Description**: Camera adopts the visual perspective of a character or participant.
**When to use**: Human experience, gladiator entering arena, worker in mine.
**Camera strategy**: Handheld drift from character height.
**Spatial rule**: Viewer sees what the character sees — not the character themselves.
**FORBIDDEN**: third_person_observation.

### low_angle_hero
**Description**: Camera positioned below subject looking up to establish dominance and scale.
**When to use**: Monumental architecture, power figures, overwhelming scale.
**Camera strategy**: Slow tilt up from ground level.
**Spatial rule**: Sky or ceiling visible above subject.
**FORBIDDEN**: eye_level_for_power_shots, overhead_for_dominance.

### overhead_dominance
**Description**: Camera positioned directly above subject looking down.
**When to use**: Crowd scale, urban layouts, battle formations, architectural plans.
**Camera strategy**: Overhead descent.
**Spatial rule**: Subject fills frame from above; edges imply infinite continuation.
**FORBIDDEN**: side_angle_for_overhead_concepts.

---

## Spatial Relationship Templates

Use these templates to write the `spatial_relationship` field in the presentation plan:

- **above_below**: "The {above_element} exists directly above the {below_element} — both visible in one continuous frame"
- **inside_outside**: "The interior structure is revealed within its exterior housing — no cut required"
- **before_after**: "Past and present states share the same physical location — transition is temporal, not spatial"
- **micro_macro**: "The close detail exists within its larger context — scale readable without cutting away"

---

## Worked Example: The Colosseum Hypogeum Underground Reveal

**Scene**: Narration describes "a city hidden beneath the arena floor — tunnels, cages, and mechanical lifts that powered the spectacle above."

**Narrative goal**: Reveal the hidden city beneath the arena.

**WRONG approach**: Split the frame — arena floor on the left, underground tunnels on the right. This is `split_screen_for_two_ideas` — a FORBIDDEN DEFAULT.

**CORRECT approach**: `architectural_cutaway` — A single integrated frame in which the arena floor appears at the top and the hypogeum tunnels appear below it in the same continuous space. A slow dolly pushes through the cutaway plane. Both worlds — above and below — coexist in one frame. No cut, no split, no composited separate cameras.

**Presentation plan output**:
```json
{
  "scene_id": "s03",
  "narrative_goal": "Reveal the hidden city beneath the arena floor",
  "visual_concept": "The arena surface and the hypogeum tunnel network exist in the same vertical space",
  "presentation_method": "architectural_cutaway",
  "camera_strategy": "Single continuous dolly pushes slowly through the cutaway plane from exterior arena level down into the underground tunnels",
  "spatial_relationship": "The arena floor exists directly above the hypogeum tunnels — both visible in one continuous frame",
  "forbidden_techniques": ["split_screen", "side_by_side", "independent_camera_moves", "composited_separate_videos"]
}
```

---

## Hard Constraints for Stage B

When writing `primary_prompt`, Stage B MUST:

1. Satisfy the `presentation_method` from the scene's `presentation_plan`.
2. Include the `camera_strategy` verbatim or paraphrase it accurately.
3. Describe the `spatial_relationship` — the physical coexistence of elements in one frame.
4. Never introduce any technique listed in `forbidden_techniques`.
5. Never default to split_screen simply because two ideas need to be shown.

If a scene's presentation_plan has `presentation_method: "architectural_cutaway"`, the primary_prompt MUST describe a single integrated frame with a cutaway cross-section. It may NOT describe two separate camera feeds, two independent shots, or a split-screen composition of any kind.

---

## Rejection Criteria (Visual Intent QA)

A prompt FAILS QA if any of the following are true:

- The prompt describes two independent camera movements for a scene with a single-frame presentation method.
- The `spatial_relationship` from the presentation plan is absent from the prompt.
- The prompt introduces a `forbidden_technique` from the plan's `forbidden_techniques` list.
- An underground scene uses neither `section_cut` nor `vertical_reveal`.
- A reveal scene uses `immediate_full_reveal` or describes the subject appearing instantly without a build.
- The `presentation_method` from the plan is not recognisable in the written prompt.
