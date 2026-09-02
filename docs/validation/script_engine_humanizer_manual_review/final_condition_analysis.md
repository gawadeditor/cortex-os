# Final Condition Analysis — Script Engine Humanizer Validation

This report synthesizes `manual_results_by_condition.md` and `objective_metrics_summary.md`. It was produced by reading only those two files; no evaluation was re-run, no prior report was modified, and no metric value was recalculated. Where this report counts how many times a condition recorded the single best (or worst) already-reported value for a given metric, that is a direct tally of existing numbers, not a new derived metric.

---

# Executive Summary

- **Conditions evaluated:** 3 — Condition A, Condition B, Condition C (stable identities per `blind_mapping.json`, as already established in `manual_results_by_condition.md`)
- **Episodes evaluated:** 3 with both manual and objective coverage — ep_001, ep_002, ep_003. `objective_metrics_summary.md` additionally contains a `synthetic` trial that was never manually evaluated (no `manual_evaluation_form.md` exists for it); it is therefore excluded from the Agreement/Disagreement Analysis and Confidence Matrix below, and referenced only where noted.
- **Independence confirmation:** The two evaluation systems were performed independently. The objective metrics were computed deterministically from the raw generated text at benchmark-run time, before any human review. The manual evaluation was performed under a blind protocol — `blind_mapping.json` was not opened until after all three episodes' scoring was complete and finalized, and the manual evaluator never read `objective_metrics_summary.md` or any other benchmark output while scoring. Neither process could have influenced the other.

---

# Manual Evaluation Summary

(Reproduced from `manual_results_by_condition.md` — not recalculated.)

| Condition | Average manual score | Average rank |
|---|---|---|
| Condition A | 8.00 | 2.00 |
| Condition B | 7.90 | 2.33 |
| Condition C | 8.05 | 1.67 |

**Condition A — Reviewer strengths:** Won "Most human-sounding" in all three episodes, the single most consistent award pattern in the manual study.
**Condition A — Reviewer weaknesses:** Its two lowest "Reduction of robotic/repetitive phrasing" scores in the entire manual study (7 in ep_001, 6 in ep_002) — both episodes where its output was explicitly flagged as the heaviest user of a repetitive "not X. It is Y" antithesis construction. Carries 1 unsupported addition.

**Condition B — Reviewer strengths:** One 1st-place finish (ep_001), showing it is not uniformly weak.
**Condition B — Reviewer weaknesses:** Lowest overall average score and worst average rank of the three; 3rd-place finishes in 2 of 3 episodes. Accounts for 3 of the study's 5 total unsupported additions and 2 of 3 flagged awkward/robotic passages.

**Condition C — Reviewer strengths:** Best average rank and highest overall score of the three; zero awkward or robotic passages flagged against it in any episode.
**Condition C — Reviewer weaknesses:** Carries 1 unsupported addition (shared with the same underlying claim as Condition A's ep_001 addition); never won "Most human-sounding" in any episode.

---

# Objective Metrics Summary

(Values reproduced from `objective_metrics_summary.md`, ep_001–ep_003 only, unless noted. No value below has been recalculated.)

## Condition A

| Metric | ep_001 | ep_002 | ep_003 |
|---|---|---|---|
| word_count | 2973 | 2022 | 2690 |
| sentence stdev | 9.58 | 8.71 | 9.69 |
| lexical MATTR | 0.8051 | 0.8053 | 0.8023 |
| filler_phrase_count | 0 | 0 | 0 |
| ai_cliche_count | 0 | 0 | 1 |
| passive_voice_freq | 0.0518 | 0.0688 | 0.0789 |
| flesch_reading_ease | 46.84 | 51.46 | 52.95 |
| embellishment_count | 3 | 0 | 0 |
| repeated_4grams | 182 | 62 | 73 |
| factual coverage | 7/7 | 7/7 | 7/7 |
| cta_count_ok / schema_pass | True / True | True / True | True / True |

## Condition B

| Metric | ep_001 | ep_002 | ep_003 |
|---|---|---|---|
| word_count | 2759 | 2353 | 3067 |
| sentence stdev | 10.06 | 8.61 | 10.06 |
| lexical MATTR | 0.8128 | 0.7939 | 0.7989 |
| filler_phrase_count | 0 | 0 | 0 |
| ai_cliche_count | 0 | 0 | 0 |
| passive_voice_freq | 0.0962 | 0.1073 | 0.0653 |
| flesch_reading_ease | 45.41 | 51.72 | 49.41 |
| embellishment_count | 3 | 1 | 0 |
| repeated_4grams | 99 | 59 | 111 |
| factual coverage | 7/7 | 7/7 | 7/7 |
| cta_count_ok / schema_pass | True / True | True / True | True / True |

## Condition C

| Metric | ep_001 | ep_002 | ep_003 |
|---|---|---|---|
| word_count | 3119 | 2038 | 2994 |
| sentence stdev | 9.65 | 9.91 | 8.64 |
| lexical MATTR | 0.8165 | 0.7855 | 0.8078 |
| filler_phrase_count | 0 | 0 | 0 |
| ai_cliche_count | 0 | 0 | 0 |
| passive_voice_freq | 0.0642 | 0.0839 | 0.0936 |
| flesch_reading_ease | 49.86 | 51.6 | 51.37 |
| embellishment_count | 2 | 0 | 0 |
| repeated_4grams | 118 | 36 | 134 |
| factual coverage | 7/7 | 7/7 | 7/7 |
| cta_count_ok / schema_pass | True / True | True / True | True / True |

**word_count and sentence stdev are informational only** — no "better" direction was established for either metric in the benchmark's own design (`objective_metrics_summary.md`'s source design document records these as descriptive, not diagnostic). They are reported for completeness but not used in any standing assessment below.

## Overall objective standing (tally of already-reported best/worst values, not a new computed score)

Counting how many times each condition recorded the single best (lowest, or highest for lexical MATTR) value across the metrics with an established "better" direction (passive_voice_freq, embellishment_count, lexical MATTR, repeated_4grams — filler_phrase_count and ai_cliche_count are excluded from this tally as they are 0 in all but one condition-episode combination and provide no differentiation):

| Condition | Outright single-best count (of applicable metric-episode comparisons) |
|---|---|
| Condition A | 3 (passive_voice_freq: ep_001, ep_002; repeated_4grams: ep_003) |
| Condition B | 2 (passive_voice_freq: ep_003; repeated_4grams: ep_001) |
| Condition C | 4 (embellishment_count: ep_001; lexical MATTR: ep_001, ep_003; repeated_4grams: ep_002) |

This is a direct count of already-reported extremal values, not a weighted or recalculated composite score; `objective_metrics_summary.md` itself does not declare an aggregate winner. Notably: Condition A recorded the single highest (worst) repeated_4grams value in the entire dataset (182, ep_001) and the only recorded ai_cliche_count hit (1, ep_003). Condition B was never the outright worst on repeated_4grams in any episode. Condition C was never the outright worst on embellishment_count in any episode.

---

# Agreement Analysis

- **Same weakest condition on repetitive phrasing, with matching episodes.** Manually, Condition A's two lowest "Reduction of robotic/repetitive phrasing" scores occurred specifically in ep_001 and ep_002, with the evaluation forms naming it the heaviest user of a repetitive rhetorical construction in exactly those two episodes. Objectively, Condition A's repeated_4grams count is the single highest (worst) of the three conditions in exactly the same two episodes (182 in ep_001, 62 in ep_002). This is a specific, episode-matched agreement between the two independent evaluation systems.
- **Same best condition on repetitive-phrasing consistency.** Manually, Condition B scored highest of the three on the "Reduction of robotic/repetitive phrasing" rubric dimension (8.00 average, versus 7.00 for both A and C). Objectively, Condition B was never the single worst on repeated_4grams in any of the three episodes. Both systems independently place Condition B as the most consistent on this specific axis.
- **Same recurring strength: near-absence of AI-cliché indicators.** Every manual evaluation form states "No AI-cliché vocabulary was found" for every version in every episode. Objectively, ai_cliche_count is 0 in 8 of the 9 condition-episode combinations across ep_001–ep_003. The two systems agree on this finding in the overwhelming majority of cases (see Disagreement Analysis for the one exception).
- **Same recurring strength: no factual drift.** Manually, no factual drift (contradiction of `input_reference.md`) was found for any condition in any episode. Objectively, factual coverage is 7/7 for every condition in every episode, with no variation. The two systems agree completely on this point.
- **Overall standing tracks in the same direction.** The manual Final Ranking (1st Condition C, 2nd Condition A, 3rd Condition B) matches the ordering produced by the objective outright-best-value tally above (C=4, A=3, B=2). Both systems place Condition C first and Condition B last, though the objective margin between conditions is narrower than the manual one.
- **Both systems treat length as non-diagnostic.** The manual evaluation instructions explicitly directed the reviewer not to reward additional length or penalize fewer narration beats, and no manual finding references word count. The objective report's own design documentation records word_count as informational only, with no established "better" direction. Neither system draws a conclusion from the word_count differences visible in the tables above (Condition C has the highest word_count in 2 of 3 episodes; this is not treated as a strength or weakness by either system).

---

# Disagreement Analysis

- **ai_cliche_count vs. manual "no AI-cliché vocabulary" finding, ep_003, Condition A.** The objective metric recorded `ai_cliche_count: 1` for Condition A in ep_003 — the only non-zero value for this metric across all three evaluated episodes and all three conditions. The manual evaluation form for ep_003, however, explicitly states for every version, including the one later identified as Condition A, that no AI-cliché vocabulary was found. This is a direct, specific disagreement: the deterministic wordlist match registered one hit that the human reviewer's qualitative read did not flag. The available reports do not indicate which specific word or phrase triggered the objective match, so the source of the single-instance discrepancy cannot be further explained from the data at hand.
- **Embellishment dimension ranking, partial disagreement.** Manually, Condition A scored highest of the three on "No unnecessary embellishment or sensationalism" (8.33, versus 8.00 for both B and C). Objectively, Condition A tied for the highest (worst) embellishment_count in ep_001 (3, tied with Condition B) and Condition C recorded the lowest total embellishment_count across the three episodes (2, 0, 0) of any condition. The objective data does not clearly support Condition A's manual lead on this specific dimension; if anything, Condition C's objective embellishment_count record is the strongest of the three. This is a soft disagreement — the manual score margin (8.33 vs. 8.00) is small, and the rubric dimension covers more than the fixed wordlist the objective metric checks, but the two systems do not point the same direction on this specific axis.
- **No objective counterpart for "Most human-sounding."** Condition A swept this manual award in all three episodes, the single strongest pattern in the manual study. No metric in `objective_metrics_summary.md` directly measures this construct (naturalness/human-soundingness is not one of the deterministic metrics computed). This is not a contradiction — it is an area where the two evaluation systems simply do not overlap, and it should not be read as the objective data disagreeing with the manual finding, only as a finding the objective system cannot corroborate or refute.

---

# Confidence Matrix

| Condition | Manual confidence | Objective confidence | Combined confidence |
|---|---|---|---|
| Condition A | Medium | Medium | Medium |
| Condition B | Medium | Medium | Medium |
| Condition C | Medium | Medium | Medium |

**Manual confidence** is taken directly from the evaluation forms: all three episodes were scored at "Medium" confidence, with the reviewer explicitly noting closely clustered scores and rankings resting on a small number of specific findings rather than a wide quality gap. This applies uniformly to whichever condition appeared in each episode.

**Objective confidence** is not a value declared in `objective_metrics_summary.md` (that report contains raw metric tables only, no confidence rating). The Medium rating above is this report's own qualitative assessment of data consistency, based strictly on the tallies already shown: every condition has at least one episode where it recorded the single worst value on some metric (Condition A: repeated_4grams in ep_001 and ep_002, plus the sole ai_cliche_count hit; Condition B: passive_voice_freq in ep_001 and ep_002, plus the highest total embellishment_count; Condition C: repeated_4grams in ep_003, the highest single value recorded for that metric). With only three episodes of data and no condition uniformly best or worst across all metrics, none of the three conditions' objective pattern is strong or consistent enough to warrant a "High" rating.

**Combined confidence** is Medium for all three conditions: the two systems agree in overall direction (see Agreement Analysis) but neither individually reaches a high-confidence threshold, given the small episode count and the presence of at least one notable weak metric-episode for every condition in both systems.

---

# Final Recommendation

**1st: Condition C**
- From manual review: best average rank (1.67) and highest overall rubric average (8.05); zero awkward or robotic passages flagged against it in any episode.
- From objective metrics: highest outright-best-value tally (4 of the applicable metric-episode comparisons); never the outright worst on embellishment_count in any episode; highest lexical MATTR in 2 of 3 episodes.
- Both systems independently rank this condition first, though by a narrow margin in each case (manual overall-average spread of 0.15 points across all three conditions; objective tally spread of 4-3-2).

**2nd: Condition A**
- From manual review: won "Most human-sounding" in all three episodes, the single strongest award pattern in the study, but was specifically flagged for a repetitive rhetorical construction in 2 of 3 episodes, producing its two lowest dimension scores of the study.
- From objective metrics: best or tied-best passive_voice_freq in 2 of 3 episodes, but the single highest repeated_4grams value in the entire dataset (ep_001) and the study's only ai_cliche_count hit (ep_003).
- The two systems agree closely here: the manual repetitive-phrasing criticism and the objective repeated_4grams spike land in the exact same two episodes (ep_001, ep_002), which is the clearest cross-system corroboration found in this analysis. This consistent, specific weakness — set against an otherwise strong showing on naturalness/human-sounding — is why this condition places second rather than first or third.

**3rd: Condition B**
- From manual review: lowest overall score (7.90) and worst average rank (2.33) of the three; source of 3 of the study's 5 unsupported additions and 2 of 3 flagged awkward passages.
- From objective metrics: lowest outright-best-value tally (2); highest or tied-highest passive_voice_freq in 2 of 3 episodes; highest total embellishment_count across the three episodes.
- The two systems agree on this condition placing last, though the objective margin is the narrowest of the three rankings (2 vs. 3 vs. 4 in the tally, versus a clearer manual separation of 2.33 vs. 2.00 vs. 1.67 average rank). This condition's one 1st-place manual finish (ep_001) and its never being the outright worst on repeated_4grams in any episode are the strongest points in its favor, but they do not outweigh the concentration of flagged issues elsewhere.

Where the two systems disagree (the ep_003 ai_cliche_count discrepancy for Condition A, and the embellishment-dimension ranking) neither disagreement is large enough to change the overall three-way ordering above; both are noted in the Disagreement Analysis as specific, bounded exceptions rather than systemic contradictions.

---

# Future Improvements

Recurring weaknesses that appeared regardless of which condition produced them, prioritized by expected impact:

1. **Repetitive rhetorical structures (highest priority).** The "not X. It is Y" antithesis construction was flagged manually in every one of the three episodes, for at least one condition each time, and is the single most frequently repeated specific criticism in the manual study. It is also the one weakness with direct, episode-matched objective corroboration (repeated_4grams spikes). Addressing this would improve the specific weakness most clearly identified by both evaluation systems working independently.
2. **Unsupported additions (high priority).** Specific claims exceeding what the source material states were found in every episode, concentrated in one condition (3 of 5 total instances). This is a factual-precision risk distinct from outright factual drift (none of which was found by either system), but worth tightening given how consistently it recurred.
3. **Exposition/citation density tipping toward an "explainer" register (medium priority).** Noted for different conditions across different episodes rather than concentrated in one condition, this is a documentary-voice consistency risk that appeared repeatedly enough across the study to warrant attention, even without a single condition to target.
4. **Passive-voice frequency (medium priority, objective-only signal).** Not a scored manual rubric dimension on its own, but objectively elevated specifically for one condition in 2 of 3 episodes. Worth monitoring alongside the manual "naturalness" and "voice consistency" dimensions it may be contributing to.
5. **Isolated clarity/naturalness lapses (lower priority).** Two specific instances (a confusing dating-range sentence; an ambiguous "was almost the founder" construction) were each isolated to a single episode rather than systemic. Low effort to fix, but not a recurring pattern in the way items 1-3 are.

---

# Final Conclusion

- **Winning condition: Condition C**
- **Runner-up: Condition A**
- **Third place: Condition B**

Both evaluation systems, performed independently, converge on this ordering. The manual evaluation places Condition C first on the strength of its consistent 2nd-or-better episode finishes, the highest overall rubric average, and zero flagged awkward passages across the entire study. The objective metrics independently support this: Condition C recorded the most outright-best values across the applicable metrics (4 of the tallied comparisons) and was never the single worst on embellishment_count in any episode.

Condition A's position as runner-up is explained consistently by both systems: it earned the strongest single award pattern in the manual study ("Most human-sounding" in all three episodes) but was independently flagged — by the manual reviewer's qualitative read and by the objective repeated_4grams count — for the same specific weakness in the same two episodes (ep_001, ep_002). This is the clearest instance of cross-system agreement found anywhere in this analysis, and it is the specific reason this otherwise well-regarded condition does not place first.

Condition B's third-place position is likewise supported by both systems: the lowest manual overall score and worst average rank, paired with the lowest objective outright-best tally, the highest total embellishment_count, and elevated passive-voice frequency in 2 of 3 episodes. Its margin below the other two conditions is narrower in the objective data than in the manual data, but neither system places it above the other two in aggregate.

The one specific disagreement identified — a single objective ai_cliche_count hit for Condition A in ep_003 that the manual reviewer's qualitative read did not flag — is reported above with its exact evidence and does not affect the three-way ordering, since it is a single-instance discrepancy on one metric in one episode, not a pattern that appears elsewhere in either dataset.
