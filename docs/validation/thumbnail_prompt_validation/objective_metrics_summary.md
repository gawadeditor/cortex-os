# Objective Metrics Summary (Unblinded)

This document is the deterministic benchmark output, kept separate from the blind manual-review package. It uses real condition labels (A = base prompt only, B = base + internal Thumbnail Intelligence skill, C = production: base + Thumbnail Intelligence + GPT-Image2 skill) — it is not part of the blind review and does not correspond 1:1 with the "Version A/B/C" labels used there.

## ep_001

Mode: LEGACY_SCENE_MODE (identical across A/B/C for this episode)

| Metric | A (base) | B (base+TI) | C (production) |
|---|---|---|---|
| 1. schema_pass | True | True | True |
| 2. valid_candidate_count | 3 | 3 | 3 |
| 3. candidate_distinctness | 0.4639 | 0.3555 | 0.4833 |
| 4. semantic_overlap | 0.7588 | 0.8415 | 0.7588 |
| 5. emotional_trigger_distinctness | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) |
| 6. curiosity_gap_distinctness | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) |
| 7. viewer_promise_distinctness | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) |
| 8. focal_subject_clarity | 1.0 | 1.0 | 1.0 |
| 9. single_dominant_subject_compliance | 1.0 | 1.0 | 1.0 |
| 10. composition_plan_completeness | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) |
| 11. typography_plan_completeness | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) |
| 12. text_zone_clarity | 1.0 | 1.0 | 1.0 |
| 13. mobile_legibility (fraction passed) | 0.0 | 0.0 | 0.0 |
| 14. image_prompt_specificity | 0.0 | 0.3333 | 0.0 |
| 15. image_prompt_actionability | 1.0 | 1.0 | 1.0 |
| 16. forbidden_layout_violations | 0 | 0 | 0 |
| 17. duplicate_visual_concept_risk (mean overlap) | 0.7588 | 0.8415 | 0.7588 |
| 18. identity_brand_violations | 3 | 2 | 3 |
| 19. unsupported_invention_risk | 0.3402 | 0.3671 | 0.3679 |
| 20. deterministic_validators (pre-gen pass) | True | True | True |
| 20. deterministic_validators (CTR pass) | True | True | True |
| 20. deterministic_validators (zones pass) | False | False | False |
| 21. prompt_approx_tokens | 10797 | 11394 | 14272 |
| 22. output_approx_tokens | 700 | 670 | 702 |
| 23. latency_ms | 319428.1 | 337337.0 | 349282.1 |

## ep_002

Mode: LEGACY_SCENE_MODE (identical across A/B/C for this episode)

| Metric | A (base) | B (base+TI) | C (production) |
|---|---|---|---|
| 1. schema_pass | True | True | True |
| 2. valid_candidate_count | 3 | 3 | 3 |
| 3. candidate_distinctness | 0.3822 | 0.5571 | 0.4178 |
| 4. semantic_overlap | 0.8195 | 0.7168 | 0.7953 |
| 5. emotional_trigger_distinctness | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) |
| 6. curiosity_gap_distinctness | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) |
| 7. viewer_promise_distinctness | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) | N/A (legacy_scene_mode) |
| 8. focal_subject_clarity | 1.0 | 1.0 | 1.0 |
| 9. single_dominant_subject_compliance | 1.0 | 1.0 | 1.0 |
| 10. composition_plan_completeness | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) |
| 11. typography_plan_completeness | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) | N/A (not applicable outside concept_driven_mode) |
| 12. text_zone_clarity | 1.0 | 1.0 | 1.0 |
| 13. mobile_legibility (fraction passed) | 0.0 | 0.0 | 0.0 |
| 14. image_prompt_specificity | 0.3333 | 0.3333 | 0.0 |
| 15. image_prompt_actionability | 1.0 | 1.0 | 1.0 |
| 16. forbidden_layout_violations | 0 | 0 | 0 |
| 17. duplicate_visual_concept_risk (mean overlap) | 0.8195 | 0.7168 | 0.7953 |
| 18. identity_brand_violations | 0 | 0 | 0 |
| 19. unsupported_invention_risk | 0.4582 | 0.5351 | 0.5185 |
| 20. deterministic_validators (pre-gen pass) | True | True | True |
| 20. deterministic_validators (CTR pass) | True | True | True |
| 20. deterministic_validators (zones pass) | False | False | False |
| 21. prompt_approx_tokens | 10797 | 11394 | 14272 |
| 22. output_approx_tokens | 679 | 651 | 685 |
| 23. latency_ms | 282015.6 | 346922.6 | 289238.4 |

## ep_003

Mode: CONCEPT_DRIVEN_MODE (identical across A/B/C for this episode)

| Metric | A (base) | B (base+TI) | C (production) |
|---|---|---|---|
| 1. schema_pass | True | True | True |
| 2. valid_candidate_count | 3 | 3 | 3 |
| 3. candidate_distinctness | 0.4452 | 0.4727 | 0.4389 |
| 4. semantic_overlap | 0.8674 | 0.8488 | 0.8465 |
| 5. emotional_trigger_distinctness | 3 | 3 | 3 |
| 6. curiosity_gap_distinctness | 0.963 | 0.963 | 0.963 |
| 7. viewer_promise_distinctness | 0.9722 | 0.9722 | 0.9722 |
| 8. focal_subject_clarity | 1.0 | 1.0 | 1.0 |
| 9. single_dominant_subject_compliance | 1.0 | 1.0 | 1.0 |
| 10. composition_plan_completeness | 1.0 | 1.0 | 1.0 |
| 11. typography_plan_completeness | 1.0 | 1.0 | 1.0 |
| 12. text_zone_clarity | 1.0 | 1.0 | 1.0 |
| 13. mobile_legibility (fraction passed) | 1.0 | 1.0 | 1.0 |
| 14. image_prompt_specificity | 0.0 | 0.0 | 0.0 |
| 15. image_prompt_actionability | 1.0 | 1.0 | 1.0 |
| 16. forbidden_layout_violations | 0 | 0 | 0 |
| 17. duplicate_visual_concept_risk (mean overlap) | 0.8674 | 0.8488 | 0.8465 |
| 18. identity_brand_violations | 0 | 3 | 3 |
| 19. unsupported_invention_risk | 0.7302 | 0.75 | 0.6853 |
| 20. deterministic_validators (pre-gen pass) | True | True | True |
| 20. deterministic_validators (CTR pass) | True | True | True |
| 20. deterministic_validators (zones pass) | True | True | True |
| 21. prompt_approx_tokens | 10797 | 11394 | 14272 |
| 22. output_approx_tokens | 2342 | 2306 | 2438 |
| 23. latency_ms | 658265.5 | 273819.2 | 403574.3 |
