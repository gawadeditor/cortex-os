# Thumbnail Prompt Writer — Before vs After Comparison

**Legacy system** = Condition A (base `thumbnail_director.txt` prompt only).
**New system** = Condition C (production: base + Thumbnail Intelligence skill + GPT-Image2 skill).
Condition B (base + Thumbnail Intelligence only) is an intermediate build; it is reported where relevant but is not the subject of the replace/keep decision.

Mapping source: `blind_mapping.json` (opened after the blind phase was declared complete, as required to translate blind Version A/B/C labels back to real conditions A/B/C). The mapping is identical in all three episodes: blind **Version A = real C** (new), blind **Version B = real B** (intermediate), blind **Version C = real A** (legacy).

---

## 1. Executive conclusion

**Mixed.** The new system (C) does not show a broad, consistent quality improvement over the legacy system (A). It wins or ties the manual blind review in 2 of 3 episodes and objectively improves candidate diversity in 2 of 3 episodes, but it objectively regresses on brand-identity violations (ep_003: legacy 0 vs new 3) and on unsupported-invention risk in 2 of 3 episodes (ep_001, ep_002). Composition quality, typography quality, and generation readiness are objectively tied in every episode.

---

## 2. Manual evaluation comparison

| Episode | Legacy (blind label → real A) | New winner (blind label → real C) | Result |
|---|---|---|---|
| ep_001 | Version C — dimension 74/100, candidate 23/30 | Version A — dimension 78/100, candidate 22/30 | Split: dimension total favors new, candidate-level total favors legacy |
| ep_002 (audit-grade) | Version C — dimension 75/100, candidate 22/30 | Version A — dimension 74/100, candidate 22/30 | Legacy slightly ahead; candidate totals tied |
| ep_003 (audit-grade Rev. 2) | Version C — dimension 79/100, candidate 23/30 | Version A — dimension 79/100, candidate 23/30 | Fully tied |

**Where the improvement is real:** In ep_001, the new condition led on the dimension-level rubric: "Version A leads primarily on candidate-set distinctness and headline variety" (ep_001 form, Final Ranking section). This is the only episode where the new condition shows an unambiguous manual-review lead on any total.

**Where there is no measurable improvement:** In ep_002 (audit-grade), the legacy condition scored higher: "1st: Version C (dimension total 75/100...); 2nd: Version A (dimension total 74/100...)" with candidate totals tied at 22/30 each. In ep_003 (audit-grade Revision 2), the two conditions are exactly tied: "1st (tied): Version A... 1st (tied): Version C... dimension total 79/100; candidate total 23/30" for both. The audit-grade evaluation for ep_003 further notes: "Versions A and C are fully tied with no quote-supported distinction between them at any point in this evaluation."

**Methodology caveat:** ep_001's manual evaluation was completed under the original (non-audit-grade) protocol, before the stricter audit-grade rules (exact-quotation requirement, no style-based inference) were introduced for ep_002 and ep_003. ep_001's 78-vs-74 dimension-level gap is therefore not directly comparable in rigor to ep_002/ep_003's tied or near-tied audit-grade results, and may overstate the new condition's advantage relative to what a re-audit would show.

---

## 3. Objective metrics comparison (Condition A = legacy vs Condition C = new)

| Category | ep_001 (A vs C) | ep_002 (A vs C) | ep_003 (A vs C) | Net |
|---|---|---|---|---|
| **Prompt quality** (image_prompt_specificity, actionability, zones-pass) | specificity 0.0 vs 0.0 (tied); actionability 1.0 vs 1.0 (tied); zones-pass False vs False (tied) | specificity **0.3333 vs 0.0** (legacy higher); actionability tied; zones-pass tied False | specificity 0.0 vs 0.0 (tied); actionability tied; zones-pass tied True | Tied in 2 episodes; legacy ahead in ep_002 |
| **Candidate diversity** (candidate_distinctness, semantic_overlap) | distinctness 0.4639 vs **0.4833** (new higher); overlap 0.7588 vs 0.7588 (tied) | distinctness 0.3822 vs **0.4178** (new higher); overlap **0.8195 vs 0.7953** (new lower/better) | distinctness **0.4452 vs 0.4389** (legacy higher); overlap **0.8674 vs 0.8465** (new lower/better) | New ahead in 2 of 3 episodes |
| **Composition quality** (focal_subject_clarity, single_dominant_subject_compliance, composition_plan_completeness) | 1.0 vs 1.0 (tied) | 1.0 vs 1.0 (tied) | 1.0 vs 1.0 on all three sub-metrics (tied) | Fully tied, all episodes |
| **Typography quality** (typography_plan_completeness, mobile_legibility) | mobile_legibility 0.0 vs 0.0 (tied) | mobile_legibility 0.0 vs 0.0 (tied) | typography_plan_completeness 1.0 vs 1.0 (tied); mobile_legibility 1.0 vs 1.0 (tied) | Fully tied, all episodes |
| **Generation readiness** (schema_pass, valid_candidate_count, deterministic_validators) | True/3/True (tied) | True/3/True (tied) | True/3/True (tied) | Fully tied, all episodes |
| **Historical grounding** (unsupported_invention_risk, lower = better) | 0.3402 vs **0.3679** (legacy lower/better) | 0.4582 vs **0.5185** (legacy lower/better) | **0.7302 vs 0.6853** (new lower/better) | Legacy ahead in 2 of 3 episodes |
| **Visual identity consistency** (identity_brand_violations, lower = better) | 3 vs 3 (tied) | 0 vs 0 (tied) | **0 vs 3** (legacy strictly better) | Legacy ahead or tied in all 3; strictly ahead in ep_003 |
| **Click potential** | No objective metric measures this directly. | | | See Section 2 (manual review is the only signal) |

The `mobile_legibility` jump from 0.0 (ep_001, ep_002) to 1.0 (ep_003) is a **mode effect**, not a condition effect: both episodes report "Mode: LEGACY_SCENE_MODE (identical across A/B/C for this episode)" and ep_003 reports "Mode: CONCEPT_DRIVEN_MODE (identical across A/B/C for this episode)" — the value is identical for A and C within each episode, so it does not reflect a legacy-vs-new difference.

---

## 4. What specifically became better?

- **Candidate diversity**, measured by `candidate_distinctness` and `semantic_overlap`: new (C) scores better in 2 of 3 episodes (ep_001, ep_002 on distinctness; ep_002, ep_003 on overlap).
- **ep_001 manual dimension-level score**: new condition scored higher (78 vs 74), per the non-audit-grade ep_001 review's own stated basis ("candidate-set distinctness and headline variety").

## 5. What became worse?

- **Visual identity consistency** in ep_003: `identity_brand_violations` is 0 for legacy and **3** for new — a concrete regression on a hard-constraint metric.
- **Unsupported-invention risk** in ep_001 and ep_002: new scores higher (worse) in both — 0.3679 vs 0.3402 (ep_001) and 0.5185 vs 0.4582 (ep_002).
- **Image-prompt specificity** in ep_002: new scores 0.0 vs legacy's 0.3333.
- **Manual review** in ep_002 (audit-grade): legacy outscored new on the dimension total (75 vs 74).

## 6. What stayed unchanged?

- **Composition quality** — identical across legacy and new in every episode (`focal_subject_clarity`, `single_dominant_subject_compliance`, `composition_plan_completeness` all 1.0/1.0).
- **Typography quality** — identical across legacy and new in every episode (`typography_plan_completeness` 1.0/1.0 where applicable; `mobile_legibility` matched within each episode).
- **Generation readiness** — identical across legacy and new in every episode (`schema_pass`, `valid_candidate_count`, `image_prompt_actionability`, `deterministic_validators` pass rates all matched).
- **Manual review outcome in ep_003** — exactly tied.

## 7. Remaining weaknesses in the new prompt system

- `identity_brand_violations` regressed from 0 to 3 in ep_003 relative to legacy, with no compensating quality signal identified in either the manual or objective data.
- `unsupported_invention_risk` is higher than legacy in 2 of 3 episodes.
- `image_prompt_specificity` is 0.0 for the new condition in 2 of 3 episodes (ep_001, ep_003), and lower than legacy in ep_002.
- `mobile_legibility` is 0.0 for both legacy and new in ep_001 and ep_002 (LEGACY_SCENE_MODE) — this is a shared, unresolved weakness, not something the new system fixed.
- `deterministic_validators (zones pass)` fails for both legacy and new in ep_001 and ep_002 — also an unresolved shared weakness.
- The manual evaluation's rigor is inconsistent across episodes (ep_001 non-audit-grade vs ep_002/ep_003 audit-grade), which limits confidence in the ep_001 result specifically.

## 8. Is the improvement negligible, small, moderate, or major?

**Negligible.** Composition quality, typography quality, and generation readiness are fully tied across every episode with no exceptions. The one real, repeated gain (candidate diversity, 2 of 3 episodes) is offset by real, repeated regressions on unsupported-invention risk (2 of 3 episodes) and a concrete brand-violation regression (ep_003: 0 → 3). The manual blind review — the only signal available for click potential — is tied or favors legacy in 2 of 3 episodes under the stricter audit-grade protocol, and favors the new condition in only the one episode reviewed under the looser, earlier protocol. No category shows a improvement large enough, or consistent enough across all three episodes, to justify "small," "moderate," or "major."

## 9. Production recommendation

**KEEP NEW WITH MAJOR TUNING**

The diversity gain is real but does not offset the brand-violation and invention-risk regressions found in the majority of tested episodes. The `identity_brand_violations` regression in ep_003 (0 → 3) touches a hard constraint category (brand/visual-identity safety), not a soft quality preference, and warrants investigation and correction before the added skill stack (Thumbnail Intelligence + GPT-Image2) can be considered a validated net improvement over the base prompt.

## 10. Final decision

**NO.** The evidence does not support an unconditional replacement of the legacy (base-prompt) system with the new (production) skill stack. Two of three episodes show the new system tied with or trailing legacy on the manual blind review, and two objective categories (historical grounding, visual identity consistency) show the new system tied-or-worse in the majority of episodes tested, including one concrete brand-violation regression. The new system should remain in use for further tuning and re-testing, not certified as a proven replacement.
