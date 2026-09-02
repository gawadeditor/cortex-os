# Script Engine Humanizer — Manual Review Package

This package supports a blind manual quality review comparing three Script
Engine output conditions across four trials. Reviewers should not know which
condition is which until the reveal.

## How to review

1. For each trial folder (`synthetic/`, `ep_001/`, `ep_002/`, `ep_003/`), read
   `input_reference.md` first — it describes the source material only (topic,
   facts, structure) and never reveals how any version was produced.
2. Read `version_a.md`, `version_b.md`, and `version_c.md` in that folder.
   These are the same script rendered three different ways. They contain no
   labels indicating how each was produced.
3. Fill out that folder's `manual_evaluation_form.md` using the fixed rubric —
   score every dimension for every version, with a quoted passage as evidence.
4. Repeat for all four trials.
5. **Do not open `blind_mapping.json`** until you are ready for the reveal —
   it maps each trial's Version A/B/C labels to the real condition. Opening
   it early will bias the evaluation.

## What NOT to do until the reveal

Do not guess, discuss, or record which version you believe used which
approach. Score only on the text in front of you.

## Files

- `README.md` — this file
- `blind_mapping.json` — the randomized version→condition mapping (do not open before scoring)
- `combined_manual_evaluation_form.md` — all four trials' evaluation forms in one document, for convenience
- `objective_metrics_summary.md` — the SEPARATE, unblinded, deterministic benchmark metrics (real condition labels; not part of the blind review)
- `synthetic/`, `ep_001/`, `ep_002/`, `ep_003/` — one folder per trial, each containing `input_reference.md`, `version_a.md`, `version_b.md`, `version_c.md`, `manual_evaluation_form.md`
