# Script Engine Humanizer Validation

## Status

Completed

## Selected Condition

Condition C

## Why it was selected

- Best average rank (1.67) and highest overall manual rubric average (8.05) of the three conditions
- 2nd-or-better finish in every manually evaluated episode, with zero awkward or robotic passages flagged against it in any episode
- Highest outright-best-value tally across the objective metrics (4 of the applicable metric-episode comparisons)
- Never the outright worst on embellishment_count in any episode, and lowest total embellishment_count across the three episodes
- Highest lexical diversity (MATTR) in 2 of 3 episodes
- Fewest unsupported additions of the three conditions (1, tied with Condition A, versus 3 for Condition B)
- Both independent evaluation systems (manual and objective) converged on the same first-place ranking

## Known Limitations

- Carries 1 unsupported addition (a Cicero-Rhodes visit date not present in the source reference)
- Never won "Most human-sounding" in any episode
- Recorded the single highest (worst) repeated_4grams value in one episode (ep_003)

## Future Improvements

- Reduce repetitive rhetorical structures (the "not X. It is Y" antithesis construction), the most frequently repeated criticism across the validation
- Reduce unsupported additions — specific claims exceeding the source material
- Reduce exposition/citation density that occasionally tips toward an "explainer" register
- Monitor passive-voice frequency alongside naturalness and voice-consistency scoring
- Address isolated clarity/naturalness lapses where identified

## Decision

Condition C becomes the temporary default Script Humanizer configuration for Hypogeum until future validation supersedes it.
