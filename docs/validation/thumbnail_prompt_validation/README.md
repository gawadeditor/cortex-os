# Thumbnail Prompt Writer — Manual Review Package

This package supports a blind manual quality review comparing three Thumbnail
Prompt Writer output conditions across three real episodes. Reviewers should
not know which condition is which until the reveal.

## How to review

1. For each episode folder (`ep_001/`, `ep_002/`, `ep_003/`), read
   `input_reference.md` first — it describes the source material only (topic,
   verified entities, constraints) and never reveals how any candidate set was
   produced.
2. Read `version_a.md`, `version_b.md`, and `version_c.md` in that folder.
   These are the same episode's ranked thumbnail candidates rendered three
   different ways. They contain no labels indicating how each was produced —
   no condition names, no skill names, no system-prompt descriptions, no
   prompt-size data.
3. Fill out that folder's `manual_evaluation_form.md` using the fixed rubric —
   score every dimension for every version, with an evidence quote or field
   reference as support.
4. Repeat for all three episodes.
5. **Do not open `blind_mapping.json`** until you are ready for the reveal —
   it maps each episode's Version A/B/C labels to the real condition. Opening
   it early will bias the evaluation.

## What NOT to do until the reveal

Do not guess, discuss, or record which version you believe used which
approach. Score only on the content in front of you.

## Notes on episode modes

`ep_003` was produced in CONCEPT_DRIVEN_MODE (a real, already-existing concept
brief for that episode) — its candidates therefore show emotional trigger,
curiosity gap, viewer promise, composition plan, and typography plan fields.
`ep_001` and `ep_002` have no concept brief on disk and were produced in
LEGACY_SCENE_MODE — those fields are marked "not present" for those two
episodes' candidates, for all three versions equally. This is not a
difference between conditions; it is a difference between episodes, and it
is documented in each `input_reference.md` implicitly by the fields shown.

## Files

- `README.md` — this file
- `blind_mapping.json` — the randomized version→condition mapping (do not open before scoring)
- `objective_metrics_summary.md` — the SEPARATE, unblinded, deterministic benchmark metrics (real condition labels; not part of the blind review)
- `ep_001/`, `ep_002/`, `ep_003/` — one folder per episode, each containing `input_reference.md`, `version_a.md`, `version_b.md`, `version_c.md`, `manual_evaluation_form.md`

No image was generated to produce this package — prompt output only. No qualitative evaluation has been performed yet; the `manual_evaluation_form.md` files are intentionally blank.
