# Manual Results by Condition

This report de-anonymizes the blind manual evaluation using `blind_mapping.json`. It was produced by reading only `blind_manual_summary.md` and `blind_mapping.json` — `objective_metrics_summary.md`, `metrics.json`, and all other benchmark outputs were not opened, read, or used. No score, ranking, or evaluation content was modified; this report only re-derives condition-level aggregates from the per-version numbers already recorded in the three completed `manual_evaluation_form.md` files.

**Important structural note:** `blind_mapping.json` shows the Version A / Version B / Version C labels were **independently randomized per episode**, not a single fixed mapping across the study. "Version A" in ep_001 is a different underlying condition from "Version A" in ep_002 or ep_003. All aggregation below is therefore done at the **condition** level (A, B, C — the stable identities), using the correct per-episode mapping, not at the label level.

---

# Condition Mapping

| Episode | Version A | Version B | Version C |
|---|---|---|---|
| ep_001 | Condition C | Condition A | Condition B |
| ep_002 | Condition B | Condition A | Condition C |
| ep_003 | Condition A | Condition C | Condition B |

Read as: in ep_001, the text file labeled "Version A" was Condition C's output, "Version B" was Condition A's output, and "Version C" was Condition B's output. The mapping differs by episode; only the condition letter (A/B/C) is a stable identity across the study.

---

# Manual Evaluation Results by Condition

Per-episode condition instances, re-derived from the three evaluation forms via the mapping above:

| Condition | ep_001 rank | ep_002 rank | ep_003 rank |
|---|---|---|---|
| Condition A | 3rd | 1st | 2nd |
| Condition B | 1st | 3rd | 3rd |
| Condition C | 2nd | 2nd | 1st |

## Rank Summary

| Condition | 1st-place | 2nd-place | 3rd-place | Average rank |
|---|---|---|---|---|
| Condition A | 1 | 1 | 1 | 2.00 |
| Condition B | 1 | 0 | 2 | 2.33 |
| Condition C | 1 | 2 | 0 | 1.67 |

## Rubric Averages by Condition

| Dimension | Condition A | Condition B | Condition C |
|---|---|---|---|
| Naturalness of language | 8.67 | 7.67 | 8.00 |
| Documentary voice consistency | 8.67 | 7.67 | 8.00 |
| Reduction of robotic/repetitive phrasing | 7.00 | 8.00 | 7.00 |
| Narrative flow and engagement | 8.00 | 8.33 | 9.00 |
| Preservation of meaning and factual accuracy | 7.67 | 7.67 | 8.00 |
| Preservation of Hypogeum documentary identity | 7.67 | 8.00 | 8.33 |
| No unnecessary embellishment or sensationalism | 8.33 | 8.00 | 8.00 |
| **Overall average** | **8.00** | **7.90** | **8.05** |

---

# Awards

| Award | Condition A | Condition B | Condition C |
|---|---|---|---|
| Best opening hook | 0 | 2 | 1 |
| Best narrative flow | 1 | 0 | 2 |
| Most human-sounding | 3 | 0 | 0 |
| Most accurate | 1 | 1 | 1 |
| Most Hypogeum-consistent | 1 | 1 | 1 |

Condition A won "Most human-sounding" in all three episodes — the only award swept by a single condition across the entire study. No other award category was won by the same condition more than twice.

---

# Reviewer Findings

## Recurring strengths (apply across all conditions)

- No AI-cliché vocabulary was found in any condition's output, in any episode.
- No factual drift (outright contradiction of `input_reference.md`) was found in any condition, in any episode.
- No unnecessary embellishment or sensationalism was found in any condition — every instance scored 8 or 9 on this dimension.

## Condition-specific findings

**Condition A** — Won "Most human-sounding" in all three episodes, the single most consistent condition-level pattern in the study. However, its two lowest scores on "Reduction of robotic/repetitive phrasing" in the entire study (7 in ep_001, 6 in ep_002) both belong to this condition — in both of those episodes, the evaluation form specifically flagged this condition's instance as the heaviest user of a repetitive "not X. It is Y" antithesis construction (ep_001: "the antithesis construction recurs four times across the script — the most of the three versions — and by the fourth occurrence it reads as a rhetorical crutch"; ep_002: "the heaviest use of this device among the three versions... reads as a structural habit"). Carries 1 unsupported addition (see table below).

**Condition B** — Lowest overall average score (7.90) and worst average rank (2.33) of the three conditions, with 2 of 3 episode finishes at 3rd place (offset by a 1st-place finish in ep_001). Accounts for 3 of the study's 5 total unsupported additions and 2 of the study's 3 flagged awkward/robotic passages (a confusing dating-range sentence in ep_002; an ambiguous "was almost the founder" construction in ep_003, flagged as reading against its evident intent).

**Condition C** — Best average rank (1.67) and highest overall average score (8.05) of the three conditions. Zero awkward or robotic passages were flagged against this condition in any episode. Carries 1 unsupported addition, shared with the same underlying claim as Condition A's addition in ep_001 (see below). Never won "Most human-sounding" in any episode.

## Unsupported additions, by condition

| Episode | Condition | Description | Reviewer note |
|---|---|---|---|
| ep_001 | Condition C | States Cicero's visit to Rhodes occurred "around 79 BCE" | Specific date not present in `input_reference.md`; the same claim also appears in Condition A's ep_001 output |
| ep_001 | Condition A | States Cicero's visit to Rhodes occurred "around 79 BCE" | Specific date not present in `input_reference.md`; the same claim also appears in Condition C's ep_001 output |
| ep_002 | Condition B | States Cicero described "two devices built by Archimedes" (both attributed to the same maker) | More specific than `input_reference.md`, which does not detail Cicero's testimony beyond "the mechanism was not unique" |
| ep_003 | Condition B | States "Hipparchus worked primarily on the island of Rhodes, and Rhodes has long been considered a likely origin point for the mechanism" | Not present in `input_reference.md` or in the other two conditions' ep_003 outputs |
| ep_003 | Condition B | States Archimedes began the tradition "around 250 BCE" | `input_reference.md` specifies only "3rd-century BCE Syracuse," with no specific year given |

None of these additions were assessed as true or false — each is reported only as exceeding the specificity supplied by `input_reference.md`.

## Factual issues

No factual drift or contradiction of `input_reference.md` was found for any condition in any episode. All five identified discrepancies (table above) are additions of unsupported specificity, not contradictions of the source material.

---

# Final Manual Ranking

**1st: Condition C** — Best average rank (1.67) and highest overall rubric average (8.05) of the three conditions, with 2nd-or-better finishes in every episode and zero awkward/robotic passages flagged against it anywhere in the study. Its only identified weakness is one unsupported addition (shared with Condition A) and a complete absence of "Most human-sounding" awards.

**2nd: Condition A** — Middle average rank (2.00) and middle overall score (8.00). Distinguished by sweeping "Most human-sounding" in all three episodes — the single strongest, most consistent award pattern found for any condition — but this is offset by a recurring, explicitly-flagged weakness: in two of its three episode appearances, its output was singled out as the heaviest user of a repetitive rhetorical construction, driving its two lowest "Reduction of robotic/repetitive phrasing" scores in the entire study.

**3rd: Condition B** — Worst average rank (2.33) and lowest overall score (7.90), with 3rd-place finishes in two of three episodes. Accounts for the majority of the study's flagged issues: 3 of 5 total unsupported additions and 2 of 3 flagged awkward/robotic passages, including one sentence explicitly noted as reading against its own evident intent. Its one 1st-place finish (ep_001) shows it is not uniformly weak, but its issues are the most concentrated of any condition across the study.

This ranking is based solely on the manual evaluation forms' recorded scores, rankings, and reviewer notes. No objective benchmark metric was used or referenced, and this ranking was not merged with any automatic scoring.
