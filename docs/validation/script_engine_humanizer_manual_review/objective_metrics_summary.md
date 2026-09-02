# Objective Metrics Summary (Unblinded)

This document is the deterministic benchmark output, kept separate from the blind manual-review package. It uses real condition labels (A = production: skill + regex, B = baseline: no skill + regex, C = skill-only: skill, no regex) — it is not part of the blind review and does not correspond 1:1 with the "Version A/B/C" labels used there.

## synthetic

| Metric | A (production) | B (baseline) | C (skill-only) |
|---|---|---|---|
| word_count | 1497 | 1520 | 1830 |
| sentence stdev | 9.42 | 8.47 | 8.16 |
| lexical MATTR | 0.8167 | 0.8165 | 0.8256 |
| filler_phrase_count | 0 | 0 | 0 |
| ai_cliche_count | 0 | 0 | 0 |
| passive_voice_freq | 0.0105 | 0.0319 | 0.0261 |
| flesch_reading_ease | 53.06 | 54.0 | 53.61 |
| embellishment_count | 1 | 0 | 0 |
| repeated_4grams | 35 | 36 | 29 |
| factual coverage | 6/7 | 6/7 | 6/7 |
| cta_count_ok | True | True | True |
| schema_pass | True | True | True |

## ep_001

| Metric | A (production) | B (baseline) | C (skill-only) |
|---|---|---|---|
| word_count | 2973 | 2759 | 3119 |
| sentence stdev | 9.58 | 10.06 | 9.65 |
| lexical MATTR | 0.8051 | 0.8128 | 0.8165 |
| filler_phrase_count | 0 | 0 | 0 |
| ai_cliche_count | 0 | 0 | 0 |
| passive_voice_freq | 0.0518 | 0.0962 | 0.0642 |
| flesch_reading_ease | 46.84 | 45.41 | 49.86 |
| embellishment_count | 3 | 3 | 2 |
| repeated_4grams | 182 | 99 | 118 |
| factual coverage | 7/7 | 7/7 | 7/7 |
| cta_count_ok | True | True | True |
| schema_pass | True | True | True |

## ep_002

| Metric | A (production) | B (baseline) | C (skill-only) |
|---|---|---|---|
| word_count | 2022 | 2353 | 2038 |
| sentence stdev | 8.71 | 8.61 | 9.91 |
| lexical MATTR | 0.8053 | 0.7939 | 0.7855 |
| filler_phrase_count | 0 | 0 | 0 |
| ai_cliche_count | 0 | 0 | 0 |
| passive_voice_freq | 0.0688 | 0.1073 | 0.0839 |
| flesch_reading_ease | 51.46 | 51.72 | 51.6 |
| embellishment_count | 0 | 1 | 0 |
| repeated_4grams | 62 | 59 | 36 |
| factual coverage | 7/7 | 7/7 | 7/7 |
| cta_count_ok | True | True | True |
| schema_pass | True | True | True |

## ep_003

| Metric | A (production) | B (baseline) | C (skill-only) |
|---|---|---|---|
| word_count | 2690 | 3067 | 2994 |
| sentence stdev | 9.69 | 10.06 | 8.64 |
| lexical MATTR | 0.8023 | 0.7989 | 0.8078 |
| filler_phrase_count | 0 | 0 | 0 |
| ai_cliche_count | 1 | 0 | 0 |
| passive_voice_freq | 0.0789 | 0.0653 | 0.0936 |
| flesch_reading_ease | 52.95 | 49.41 | 51.37 |
| embellishment_count | 0 | 0 | 0 |
| repeated_4grams | 73 | 111 | 134 |
| factual coverage | 7/7 | 7/7 | 7/7 |
| cta_count_ok | True | True | True |
| schema_pass | True | True | True |
