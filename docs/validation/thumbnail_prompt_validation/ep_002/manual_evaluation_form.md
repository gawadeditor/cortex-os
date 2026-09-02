# Manual Evaluation Form — EP 002 (Audit-Grade Pass)

Evaluate Version A, Version B, and Version C independently against `input_reference.md`. Do not look at `blind_mapping.json`. Do not declare a winner until every dimension for every version is scored.

**Audit-grade protocol note:** This evaluation applies the same protocol used for the EP 003 Revision 2 audit-grade pass. Every score below is supported by an exact quotation. No inference about implementation quality, model behavior, or generation outcomes is made unless the reviewed file explicitly states it. No dimension rewards or penalizes prompt length, negative-constraint count, or prompt-engineering style. Where no direct quotation supports a distinction between versions, the entry reads "insufficient evidence" and the scores are tied.

**Shared context note (applies equally to all three versions, not a distinguishing factor):** this episode was produced without a concept brief — `concept_title`, `emotional_trigger`, `curiosity_gap`, `viewer_promise`, `focal_subject_placement`, `visual_hierarchy`, `composition_plan`, and `typography_plan` are all the literal text "*(not present...)*" in all nine candidates across all three versions. All three versions use the same three source candidates in the same rank order: `tc_001` → Rank 1, `tc_003` → Rank 2, `tc_002` → Rank 3. All nine candidates carry the identical validation line: "Zone reservation issues: image_prompt does not explicitly reserve the bottom hook text zone. Add: 'bottom 10% of frame completely empty — no scene content, no inscriptions, no symbols, no captions, no labels' to ensure PIL hook text is legible."

---

## Version A

### Central curiosity-gap alignment

- **Score (1-10):** 8
- **Evidence quote:** "directly raising the question of how this device could exist 1,400 years too early." Input: "The device that shouldn't exist for 1,400 more years."
- **Reviewer note:** This is the only version's alignment sentence to reproduce the input's specific number "1,400." It does not use the word "civilisation," present in the input's second mystery clause.

### Click potential and emotional trigger

- **Score (1-10):** 7
- **Evidence quote:** "TOO ADVANCED TO EXIST" / "THE SEA KEPT IT" / "WHO BUILT THIS MACHINE"
- **Reviewer note:** No `emotional_trigger` field exists for this episode. None of these three headlines reproduces the input's specific words "1,400" or "civilisation."

### Visual clarity at thumbnail size

- **Score (1-10):** 8
- **Evidence quote:** Rank 3 CTR rationale: "a full assembly risks becoming a less readable product shot."
- **Reviewer note:** This self-reported limitation is present in the equivalent Rank 3 candidate of all three versions (see Versions B and C below), so it does not distinguish Version A specifically.

### Candidate distinctness and concept diversity

- **Score (1-10):** 8
- **Evidence quote:** Rank 1: "within a shadowed stone chamber, blurred stone lintel doorway receding behind." Rank 2: "half-buried on silt-dark Aegean seabed beside aged wreck timber."
- **Reviewer note:** No shared setting-word overlap was found between this version's own Rank 1 and Rank 2 image prompts.

### Subject and historical accuracy

- **Score (1-10):** 6
- **Evidence quote:** Rank 1 adds "a shadowed stone chamber, blurred stone lintel doorway"; Rank 2 adds "aged wreck timber, receding ribs"; Rank 3 adds "an ancient stone workshop; tiny faceless silhouette." None of these appear in `tc_001`, `tc_002`, or `tc_003`'s one-line descriptions in `input_reference.md`.
- **Reviewer note:** Per input: "Any detail about the subject not present in the Verified Entities section or the candidate pool above should be treated as an addition beyond the source material." This applies to all three of this version's candidates and is present identically in Versions B and C.

### Composition-plan quality

- **Score (1-10):** 7
- **Evidence quote:** "**Composition plan:** *(not present — no composition_plan for this candidate)*" (all three candidates)
- **Reviewer note:** This field is absent in all nine candidates across all three versions. Insufficient evidence to support a differential score.

### Typography-plan quality

- **Score (1-10):** 7
- **Evidence quote:** "**Typography plan:** *(not present — no typography_plan for this candidate)*" (all three candidates)
- **Reviewer note:** This field is absent in all nine candidates across all three versions. Insufficient evidence to support a differential score.

### Image-prompt specificity and generation readiness

- **Score (1-10):** 7
- **Evidence quote:** *(not applicable — per audit rule, prompt length and negative-constraint count are not scored)*
- **Reviewer note:** No quote was found describing an internal contradiction or an explicit generation-related concern in this version's text. Insufficient evidence to support a differential score.

### Preservation of Hypogeum visual identity

- **Score (1-10):** 8
- **Evidence quote:** "near-black charcoal dominates, amber not background fill" (present in all three candidates). Input: "void_black / near-black must be the dominant background tone — warm amber and gold appear as accent lighting only, not as background fill."
- **Reviewer note:** Quoted text matches the input's stated constraint. No quote contradicting the constraint was found in any candidate.

### Absence of clutter, generic imagery, misleading claims, or unnecessary sensationalism

- **Score (1-10):** 8
- **Evidence quote:** *(no misleading or sensational claim located in any candidate's headline or image prompt)*
- **Reviewer note:** No quote was found in Version A contradicting "Facts That Must Not Be Invented" beyond the shared addition noted under Subject and historical accuracy above.

**Version A dimension total: 74/100**

---

## Version B

### Central curiosity-gap alignment

- **Score (1-10):** 7
- **Evidence quote:** "forcing the viewer to ask what civilisation knew and then lost." Input: "the civilisation it proves we've forgotten."
- **Reviewer note:** This sentence references "civilisation" but does not use the input's specific word "forgotten," and does not reproduce the input's specific number "1,400."

### Click potential and emotional trigger

- **Score (1-10):** 8
- **Evidence quote:** "1,400 Years Too Early" (Rank 1)
- **Reviewer note:** This is the only headline across all nine candidates in this episode to reproduce the input's specific number "1,400" verbatim.

### Visual clarity at thumbnail size

- **Score (1-10):** 8
- **Evidence quote:** Rank 3 CTR rationale: "the full gear train may become visually complex at mobile scale."
- **Reviewer note:** This self-reported limitation is present in the equivalent Rank 3 candidate of all three versions, so it does not distinguish Version B specifically.

### Candidate distinctness and concept diversity

- **Score (1-10):** 6
- **Evidence quote:** Rank 1: "inside shadowed ancient shipwreck timbers." Rank 2: "on dark Aegean seabed... beside amphora necks and ship ribs receding into sediment."
- **Reviewer note:** This version's Rank 1 and Rank 2 image prompts both contain shipwreck/underwater setting language ("shipwreck timbers" and "Aegean seabed... ship ribs"), an overlap not found between Rank 1 and Rank 2 in Versions A or C.

### Subject and historical accuracy

- **Score (1-10):** 6
- **Evidence quote:** Rank 1 adds "shadowed ancient shipwreck timbers"; Rank 2 adds "amphora necks and ship ribs"; Rank 3 adds "shadowed Hellenistic workshop doorway... tiny faceless human silhouette." None of these appear in `tc_001`, `tc_002`, or `tc_003`'s one-line descriptions.
- **Reviewer note:** Per input: "Any detail about the subject not present in the Verified Entities section or the candidate pool above should be treated as an addition beyond the source material." This applies to all three of this version's candidates and is present identically in Versions A and C.

### Composition-plan quality

- **Score (1-10):** 7
- **Evidence quote:** "**Composition plan:** *(not present — no composition_plan for this candidate)*" (all three candidates)
- **Reviewer note:** This field is absent in all nine candidates across all three versions. Insufficient evidence to support a differential score.

### Typography-plan quality

- **Score (1-10):** 7
- **Evidence quote:** "**Typography plan:** *(not present — no typography_plan for this candidate)*" (all three candidates)
- **Reviewer note:** This field is absent in all nine candidates across all three versions. Insufficient evidence to support a differential score.

### Image-prompt specificity and generation readiness

- **Score (1-10):** 7
- **Evidence quote:** *(not applicable — per audit rule, prompt length and negative-constraint count are not scored)*
- **Reviewer note:** No quote was found describing an internal contradiction or an explicit generation-related concern in this version's text. Insufficient evidence to support a differential score.

### Preservation of Hypogeum visual identity

- **Score (1-10):** 8
- **Evidence quote:** "near-black dominant 50–65%, no amber background, no museum case." Input: "void_black / near-black must be the dominant background tone."
- **Reviewer note:** Quoted text matches the input's stated constraint. No quote contradicting the constraint was found in any candidate.

### Absence of clutter, generic imagery, misleading claims, or unnecessary sensationalism

- **Score (1-10):** 8
- **Evidence quote:** *(no misleading or sensational claim located in any candidate's headline or image prompt)*
- **Reviewer note:** No quote was found in Version B contradicting "Facts That Must Not Be Invented" beyond the shared addition noted under Subject and historical accuracy above.

**Version B dimension total: 72/100**

---

## Version C

### Central curiosity-gap alignment

- **Score (1-10):** 8
- **Evidence quote:** "opening the question of what forgotten civilisation produced it." Input: "the civilisation it proves we've forgotten."
- **Reviewer note:** This is the only version's alignment sentence to reproduce both the input's specific words "forgotten" and "civilisation" together. It does not reproduce the input's specific number "1,400."

### Click potential and emotional trigger

- **Score (1-10):** 8
- **Evidence quote:** "Buried With A Civilisation" (Rank 2)
- **Reviewer note:** This is the only headline across all nine candidates in this episode to reproduce the input's specific word "civilisation" verbatim.

### Visual clarity at thumbnail size

- **Score (1-10):** 8
- **Evidence quote:** Rank 3 CTR rationale: "full assemblies can become visually busy at thumbnail scale."
- **Reviewer note:** This self-reported limitation is present in the equivalent Rank 3 candidate of all three versions, so it does not distinguish Version C specifically.

### Candidate distinctness and concept diversity

- **Score (1-10):** 8
- **Evidence quote:** Rank 1: "receding into an ancient stone chamber... doorway scale anchor." Rank 2: "within Aegean seabed wreck field... ship ribs and amphorae recede as scale anchors."
- **Reviewer note:** No shared setting-word overlap was found between this version's own Rank 1 and Rank 2 image prompts; Rank 1 does not contain wreck/ship/sea language.

### Subject and historical accuracy

- **Score (1-10):** 6
- **Evidence quote:** Rank 1 adds "ancient stone chamber... doorway scale anchor"; Rank 2 adds "ship ribs and amphorae"; Rank 3 adds "an ancient workshop... tiny faceless artisan silhouette." None of these appear in `tc_001`, `tc_002`, or `tc_003`'s one-line descriptions.
- **Reviewer note:** Per input: "Any detail about the subject not present in the Verified Entities section or the candidate pool above should be treated as an addition beyond the source material." This applies to all three of this version's candidates and is present identically in Versions A and B.

### Composition-plan quality

- **Score (1-10):** 7
- **Evidence quote:** "**Composition plan:** *(not present — no composition_plan for this candidate)*" (all three candidates)
- **Reviewer note:** This field is absent in all nine candidates across all three versions. Insufficient evidence to support a differential score.

### Typography-plan quality

- **Score (1-10):** 7
- **Evidence quote:** "**Typography plan:** *(not present — no typography_plan for this candidate)*" (all three candidates)
- **Reviewer note:** This field is absent in all nine candidates across all three versions. Insufficient evidence to support a differential score.

### Image-prompt specificity and generation readiness

- **Score (1-10):** 7
- **Evidence quote:** *(not applicable — per audit rule, prompt length and negative-constraint count are not scored)*
- **Reviewer note:** No quote was found describing an internal contradiction or an explicit generation-related concern in this version's text. Insufficient evidence to support a differential score.

### Preservation of Hypogeum visual identity

- **Score (1-10):** 8
- **Evidence quote:** "near-black dominates 50–65%, no amber/ochre background base." Input: "void_black / near-black must be the dominant background tone."
- **Reviewer note:** Quoted text matches the input's stated constraint. No quote contradicting the constraint was found in any candidate.

### Absence of clutter, generic imagery, misleading claims, or unnecessary sensationalism

- **Score (1-10):** 8
- **Evidence quote:** *(no misleading or sensational claim located in any candidate's headline or image prompt)*
- **Reviewer note:** No quote was found in Version C contradicting "Facts That Must Not Be Invented" beyond the shared addition noted under Subject and historical accuracy above.

**Version C dimension total: 75/100**

---

## Per-Candidate Evaluation

### Version A

**Rank 1 — `tc_001`**
- **Concept summary:** "Ranked first in line with VPD score 9 because the macro gear teeth make the impossible precision instantly readable at mobile scale while the chamber context avoids a product-shot artifact look."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present — this episode's plan was produced without a concept brief)*
- **Main visual subject:** "Corroded bronze gear mechanism fragment, green-brown patina, hand-cut gear teeth... within a shadowed stone chamber"
- **Composition strength:** "fills 55% of right frame, 10-degree low view. Lower-left 40% clear."
- **Text/headline strength:** "TOO ADVANCED TO EXIST"
- **Expected readability at 320px:** CTR rationale states "the macro gear teeth make the impossible precision instantly readable at mobile scale."
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "a shadowed stone chamber, blurred stone lintel doorway" is not present in `tc_001`'s description ("Extreme close-up of corroded bronze gear teeth, warm amber spotlight, deep shadow, dark void").
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 8

**Rank 2 — `tc_003`**
- **Concept summary:** "Ranked above its VPD score 7 because the seabed setting creates a stronger discovery-and-loss curiosity gap and provides visual variance from the close artifact treatment."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "half-buried on silt-dark Aegean seabed beside aged wreck timber, receding ribs anchor scale"
- **Composition strength:** "subject fills right 50%, lower-left 40% clear."
- **Text/headline strength:** "THE SEA KEPT IT"
- **Expected readability at 320px:** No explicit readability claim quoted in this candidate's CTR rationale.
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "aged wreck timber, receding ribs" is not present in `tc_003`'s description ("Mechanism fragment on Aegean seabed, warm filtered light, vast sediment surrounding it").
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 7

**Rank 3 — `tc_002`**
- **Concept summary:** "Ranked third despite VPD score 8 because a full assembly risks becoming a less readable product shot, but the tiny faceless figure adds useful human-scale and authorship intrigue as a fallback."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "arranged as a full gear train filling 60% of right frame... in an ancient stone workshop; tiny faceless silhouette at doorway"
- **Composition strength:** "filling 60% of right frame, 10-degree low view... Lower-left 40% clear"
- **Text/headline strength:** "WHO BUILT THIS MACHINE"
- **Expected readability at 320px:** CTR rationale states "a full assembly risks becoming a less readable product shot" — an explicit self-reported limitation.
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "an ancient stone workshop; tiny faceless silhouette" is not present in `tc_002`'s description ("Full gear train assembly in dramatic warm spotlight against dark void"). The figure is explicitly "faceless," consistent with input's "Do not show any human face as primary subject."
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 7

**Version A candidate total: 22/30**

### Version B

**Rank 1 — `tc_001`**
- **Concept summary:** "VPD score 9 is upheld: the close hand-cut gear teeth are instantly readable at 320px and make the impossible-technology gap visually immediate."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "monumental macro on right 45–65% from 10° low angle inside shadowed ancient shipwreck timbers"
- **Composition strength:** "lower-left 40% clear dark text zone, top 12% of frame near-black and empty for HYPOGEUM wordmark"
- **Text/headline strength:** "1,400 Years Too Early"
- **Expected readability at 320px:** CTR rationale states "the close hand-cut gear teeth are instantly readable at 320px."
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "shadowed ancient shipwreck timbers" is not present in `tc_001`'s description; this setting also duplicates the shipwreck/seabed language used in this version's own Rank 2 ("Aegean seabed... ship ribs").
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 7

**Rank 2 — `tc_003`**
- **Concept summary:** "Ranked above tc_002 despite lower VPD score 7 because the seabed context adds survival-by-chance and lost-civilisation stakes, widening the curiosity gap beyond the object itself."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "half-buried at center-right on dark Aegean seabed, small 8–10% scale beside amphora necks and ship ribs receding into sediment"
- **Composition strength:** "lower-left kept near-black for text, top 12% of frame near-black and empty for HYPOGEUM wordmark"
- **Text/headline strength:** "Buried Beneath the Sea"
- **Expected readability at 320px:** No explicit readability claim quoted in this candidate's CTR rationale.
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "amphora necks and ship ribs" is not present in `tc_003`'s description; this setting shares wreck/underwater language with this version's own Rank 1 ("shipwreck timbers").
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 6

**Rank 3 — `tc_002`**
- **Concept summary:** "Ranked third despite VPD score 8 because the full gear train may become visually complex at mobile scale, but it provides a distinct human-agency and scale-contrast fallback."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "towering macro on right 45–60%, tiny faceless human silhouette 6% height in shadowed Hellenistic workshop doorway for scale"
- **Composition strength:** "lower-left clear near-black text zone, top 12% of frame near-black and empty for HYPOGEUM wordmark"
- **Text/headline strength:** "Who Built This"
- **Expected readability at 320px:** CTR rationale states "the full gear train may become visually complex at mobile scale" — an explicit self-reported limitation.
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "shadowed Hellenistic workshop doorway... tiny faceless human silhouette" is not present in `tc_002`'s description. The figure is explicitly "faceless," consistent with the input constraint.
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 7

**Version B candidate total: 20/30**

### Version C

**Rank 1 — `tc_001`**
- **Concept summary:** "Ranked first because tc_001 has the strongest 320px readability: giant corroded teeth plus chamber depth make the impossible-age question instantly visible while avoiding a product-shot void."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "monumental right macro filling 45–65% of right frame... receding into an ancient stone chamber"
- **Composition strength:** "left 40% clear, top 12% of frame near-black and empty; doorway scale anchor"
- **Text/headline strength:** "A Machine Too Early"
- **Expected readability at 320px:** CTR rationale states "tc_001 has the strongest 320px readability."
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "an ancient stone chamber... doorway scale anchor" is not present in `tc_001`'s description.
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 8

**Rank 2 — `tc_003`**
- **Concept summary:** "Promoted above tc_002 despite its lower VPD score because the seabed wreck context adds loss, chance survival, and forgotten-civilisation stakes in a visually distinct environmental test."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "within Aegean seabed wreck field; fragment right-foreground, low seabed angle... ship ribs and amphorae recede as scale anchors"
- **Composition strength:** "left 40% clear, top 12% of frame near-black and empty"
- **Text/headline strength:** "Buried With A Civilisation"
- **Expected readability at 320px:** No explicit readability claim quoted in this candidate's CTR rationale.
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "ship ribs and amphorae" is not present in `tc_003`'s description.
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 7

**Rank 3 — `tc_002`**
- **Concept summary:** "Ranked third although VPD scored it above tc_003 because full assemblies can become visually busy at thumbnail scale; the tiny faceless artisan reframes it as human-scale impossibility."
- **Emotional trigger / Curiosity gap / Viewer promise:** *(not present)*
- **Main visual subject:** "full gear train looms right... tiny faceless artisan silhouette 8% height below"
- **Composition strength:** "left 40% clear, top 12% of frame near-black and empty"
- **Text/headline strength:** "Who Built This?"
- **Expected readability at 320px:** CTR rationale states "full assemblies can become visually busy at thumbnail scale" — an explicit self-reported limitation.
- **Expected click potential:** Insufficient evidence — no comparative claim about click potential is stated in this candidate's text.
- **Historical/factual risk:** "an ancient workshop... tiny faceless artisan silhouette" is not present in `tc_002`'s description. The figure is explicitly "faceless," consistent with the input constraint.
- **Generation risk:** Insufficient evidence — no explicit statement about generation model behavior found in this candidate's text.
- **Overall candidate score:** 7

**Version C candidate total: 22/30**

---

## Cross-Version Questions

- **Best Rank-1 candidate:** Insufficient evidence to declare a single best candidate. The only quote-supported distinction found is that Version B's Rank 1 image prompt ("inside shadowed ancient shipwreck timbers") shares setting language with its own Rank 2, a distinction not present in Version A's or Version C's Rank 1.
- **Best overall candidate across all versions:** Insufficient evidence to name a single best candidate; no candidate's own text contains a self-reported strength claim exceeding the readability claims common to all three versions' Rank 1 ("instantly readable at mobile scale" / "instantly readable at 320px" / "strongest 320px readability").
- **Strongest curiosity gap:** No `curiosity_gap` field exists for this episode. Based on the alignment-sentence text, Version A is the only one to quote the input's specific number "1,400"; Version C is the only one to quote the input's specific words "forgotten" and "civilisation" together.
- **Strongest emotional trigger:** No `emotional_trigger` field exists for this episode. Insufficient evidence to support a distinction on this basis.
- **Clearest composition:** Insufficient evidence — `composition_plan` is absent ("*(not present...)*") in all nine candidates across all three versions.
- **Best typography plan:** Insufficient evidence — `typography_plan` is absent ("*(not present...)*") in all nine candidates across all three versions.
- **Most generation-ready image prompt:** Insufficient evidence — per the audit rule, prompt length and negative-constraint density are not scored, and no other quote-supported distinction was found.
- **Most Hypogeum-consistent version:** Insufficient evidence to distinguish — each version's candidates quote matching language to the input's constraint ("void_black / near-black must be the dominant background tone... warm amber and gold appear as accent lighting only"), and no quote in any version contradicts it.
- **Any duplicated concepts (describe, with version):** Version B — Rank 1 ("inside shadowed ancient shipwreck timbers") and Rank 2 ("on dark Aegean seabed... beside amphora necks and ship ribs receding into sediment") both contain shipwreck/underwater setting language within the same version.
- **Any unclear visual hierarchy (describe, with version):** Insufficient evidence — `visual_hierarchy` is absent ("*(not present)*") in all nine candidates across all three versions, and no candidate's image prompt was found to describe conflicting focal elements.
- **Any likely mobile-legibility failure (describe, with version):** All three versions' Rank 3 candidates contain an explicit self-reported limitation: Version A, "a full assembly risks becoming a less readable product shot"; Version B, "the full gear train may become visually complex at mobile scale"; Version C, "full assemblies can become visually busy at thumbnail scale." This is a shared finding across all three versions, not a distinguishing one.
- **Any unsupported visual invention (describe, with version + quote):** Present identically across all three versions for all three ranks. Rank 1: none of the three versions' added chamber/shipwreck settings ("shadowed stone chamber," "shadowed ancient shipwreck timbers," "ancient stone chamber") appear in `tc_001`'s description. Rank 2: none of the three versions' added wreck/amphora details ("aged wreck timber," "amphora necks and ship ribs," "ship ribs and amphorae") appear in `tc_003`'s description. Rank 3: none of the three versions' added workshop/figure details appear in `tc_002`'s description.
- **Any generic or cliché concept (describe, with version + quote):** Insufficient evidence — per the audit rule, prompt-writing style is not evaluated, and no quote describes any concept as generic or cliché.

## Final Ranking

- **1st:** Version C (dimension total 75/100; candidate total 22/30)
- **2nd:** Version A (dimension total 74/100; candidate total 22/30)
- **3rd:** Version B (dimension total 72/100; candidate total 20/30)

This ranking rests on two categories of quote-supported distinction. First, at the alignment/headline level: Version A is the only version to reproduce the input's specific number "1,400" (alignment sentence and Rank 1 headline); Version C is the only version to reproduce the input's specific words "forgotten" and "civilisation" together (alignment sentence and Rank 2 headline); Version B reproduces neither pairing as closely in its alignment sentence, though its Rank 1 headline does quote "1,400 Years Too Early" verbatim. Second, and more decisive at the candidate level: Version B's Rank 1 and Rank 2 candidates are the only pair, in any version, that share shipwreck/underwater setting language within the same version ("shipwreck timbers" in Rank 1; "Aegean seabed... ship ribs" in Rank 2), a quote-supported distinctness finding that accounts for Version B's lower scores on Candidate distinctness and concept diversity and on both affected candidates' individual scores.

- **Reviewer confidence (low / medium / high):** Low — the margins between all three versions are narrow (75/74/72 dimension; 22/22/20 candidate), and the ranking depends on a small number of specific quotations (a numeral match, a keyword pairing, and one within-version setting overlap) rather than a broad, independently-confirmed quality gap.
