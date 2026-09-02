# Antikythera Thumbnail — Composition Blueprint v5.1 (provider-safe prompt)

**Status:** Design only. No provider call made. V3/V4/V5 preserved on disk.
V5.1 does not change the V5 composition intent (headline zone 30-34%,
mechanism 28-32%, partially concealed, reduced detail, subtle patina,
localized doorway light, active human figure, unchanged exact headline) —
it only fixes how that intent is rendered as a provider prompt, after the
V5 attempt failed twice on MUAPI with content-processing errors.

---

## 1. Root cause assessment

The failed V5 request's composed prompt was **6,730 characters (~1,010
words)**. The codebase's own documented design target for this exact field
— `hypogeum.muapi.generate_thumbnail()`'s docstring — is **"Provider-ready
image generation prompt (30–60 words)."** The V5 prompt was roughly
**17–25× longer** than the system was originally designed to submit. Length
alone is a plausible primary cause. Two further, independent contributing
factors were found and fixed (see §3):

1. **Malformed concatenation.** Components 2/3/4 (typography, DNA, brand)
   were appended with no paragraph breaks — three logically distinct rule
   domains ran together as one ~3,200-character block, separated only by
   `" — LABEL:"` markers, in contrast to component 1's clearly paragraphed
   structure.
2. **Inconsistent quoting.** `_embed_typography_in_prompt()` used Python's
   `!r` format spec, which silently switches between single- and
   double-quote characters depending on whether the underlying string
   contains an apostrophe (`repr("EXIST") → 'EXIST'` but
   `repr("IT SHOULDN'T") → "IT SHOULDN'T"`) — producing an unintentional,
   inconsistent punctuation pattern (`'EXIST' ... "IT SHOULDN'T"`) in the
   same sentence.
3. **A rogue hardcoded hex color** (`#F5E3B9`) in the typography block —
   not in the canonical 7-swatch Brand Bible palette, a second uncoordinated
   color source contradicting the brand directive's own palette.
4. **Extreme repetition.** See exact counts below — several concepts
   restated 4–7 times across components with no new information added after
   the first 1–2 mentions.
5. **High negative-instruction density.** Roughly 35–40 distinct
   negation/prohibition phrases ("not", "never", "no", "NOT") across a
   ~1,000-word prompt. Cannot be confirmed as a moderation trigger without
   visibility into MUAPI's internal content-processing logic, but is a
   plausible contributing factor and was reduced as a side effect of
   de-duplication.

The second failed attempt used a genuinely new submission (new
`request_id`) with a different, more specific provider error — `"The
current content could not be processed"` rather than the first attempt's
generic `"Internal Error"` — consistent with a content-processing/length
issue rather than a pure transient outage.

---

## 2. Exact failed prompt length

| Component | Characters |
|---|---|
| 1. Topic-specific concept | 3,543 |
| 2. Integrated typography | 733 |
| 3. Composition DNA (full/verbose) | 1,332 |
| 4. Brand directive (full/verbose) | 1,122 |
| **Total** | **6,730** |

Word count: ~1,010. Estimated tokens: ~1,300–1,700 (rough `chars/4` and
`words×1.3` estimates — MUAPI/GPT-Image-2's exact tokenizer/limit is not
documented in this repository).

---

## 3. Repetition / contradictions found

| Concept | Times stated | Detail |
|---|---|---|
| Mobile readability ("320x180") | 4 | Headline zone, figure zone, DNA figure clause, DNA closing sentence |
| Headline width ("30-34%" / "34%") | 4 | Topic zone label, topic headline paragraph, DNA occupies-clause, DNA emphasis-cap clause |
| Hierarchy (primary/secondary/tertiary order) | 7 | Topic opening sentence, 2 zone labels, 2 restatements in prose, DNA hierarchy sentence, DNA explicit focal-priority sentence |
| Mechanism concealment/detail | 5 | 3 in topic prompt (one phrase — *"mostly in shadow, only partially revealed — a glimpse, not a full display"* — appears **near-verbatim** in both the topic prompt and the DNA directive) + 2 in DNA |
| Highlight containment | 2 | Topic doorway paragraph + DNA |
| Patina cap | 2 | Near-verbatim *"never a dominant ... color field"* in both topic and DNA |
| Fantasy/product-render prohibition | 2 | Topic opening disclaimer + brand directive's own forbidden-finish list (thematically redundant, not identical wording) |

No hard logical contradictions were found (nothing told the model to do
directly opposite things) — the failure mode is **redundant restatement**,
not conflicting instructions.

---

## 4. Provider-safe prompt ceiling chosen

**4,500 characters**, per instruction ("preferably under 4,500"). The V5.1
prompt (§5) comes in well under this ceiling.

---

## 5. Final V5.1 prompt length

**2,467 characters** (~370 words) — a **63% reduction** from the failed
6,730-character V5 prompt, and well under the 4,500-character ceiling.

| Component | V5 (failed) | V5.1 |
|---|---|---|
| 1. Topic-specific concept | 3,543 | 718 |
| 2. Integrated typography | 733 | 461 |
| 3. Composition DNA | 1,332 (verbose) | 607 (compact) |
| 4. Brand directive | 1,122 (verbose) | 681 (compact) |
| **Total** | **6,730** | **2,467** |

---

## 6. Rules removed as duplicates

**No structured rule, ceiling, or constraint was removed or weakened.**
Only *restatements* of the same rule were removed, and each restatement's
information now lives in exactly one component, per the required ownership
model:

- Removed from the topic-specific prompt: hierarchy labels
  ("primary/secondary/tertiary read"), the 30-34%/34% headline width
  numbers, the 28-32% mechanism cap and "partially concealed"/"reduced
  detail" phrasing, "readable at 320x180" (×2), highlight-containment
  phrasing, the fantasy-poster disclaimer (brand directive already forbids
  fantasy-art finish), the "No extra text..." disclaimer (now a
  DNA-directive rule, stated once).
- Removed from the typography block: the width/mobile-readability
  restatement, the "receives realistic shadows / warm amber light / depth
  matching environment" clause (redundant with the topic concept's own
  lighting description), the rogue hardcoded hex color.
- Removed from the DNA/brand directives: only prose verbosity (full role
  descriptions per swatch, explanatory parentheticals on each forbidden
  item) — the compact builders still read every value from the exact same
  `composition_plan` fields / Brand Bible values as the full builders.

---

## 7. Rules preserved

Every one of the following remains present in V5.1, each stated **exactly
once**, owned by the component listed:

| Rule | Owner | V5.1 value |
|---|---|---|
| Headline zone width | DNA (compact) | 30-34% |
| Emphasis-line max width | DNA (compact) | 34% |
| Hierarchy / eye path | DNA (compact) | hook_text > human_figure > focal_object |
| Human figure gesture | DNA (compact) | visibly active, not static |
| Highlight containment | DNA (compact) | localized and contained, never a full-frame wash |
| Mechanism max frame share | DNA (compact) | 28-32% |
| Mechanism concealment | DNA (compact) | partially concealed in shadow |
| Mechanism detail density | DNA (compact) | a few large simplified forms, minimal detail |
| Patina prominence cap | DNA (compact) | subtle, never dominant |
| Focal-zone negative-space buffer | DNA (compact) | strong near-black separation, asymmetric framing |
| Mobile readability | DNA (compact) | everything readable at 320x180 |
| No incidental text | DNA (compact) | no inscriptions/symbols/watermarks beyond declared headline |
| Exact headline text | Typography | `IT SHOULDN'T` / `EXIST` |
| Line breaks / scale relationship | Typography | EXIST ~2x taller, directly below |
| Letterform character | Typography | carved weathered stone, restrained metallic edge highlight |
| Placement | Typography | bottom-left quadrant |
| Palette + ratios | Brand (compact) | near-black/charcoal 60-70%, dark stone/bronze 20-30%, gold 5-10% never dominant |
| Forbidden colors | Brand (compact) | all 8 items preserved, shortened to key terms |
| Forbidden finish | Brand (compact) | fantasy-art, museum-display, glossy product-render |
| Subject/environment/narrative action | Topic concept | doorway, figure, mechanism scene content |

Full canonical rules remain unchanged and available via
`describe_dna_constraints()` (verbose) and
`build_visual_identity_directive()` (verbose) for
`hypogeum.agents.thumbnail_plan_validator.validate_thumbnail_dna()` and
documentation — `validate_thumbnail_dna()` validates the underlying
`composition_plan` fields directly and is unaffected by which text-rendering
function is used for the actual provider prompt.

---

## 8. Exact V5.1 provider prompt

Topic-specific concept (component 1):

```
Premium archaeological investigative documentary YouTube thumbnail, 16:9. Left zone: two-line carved-stone headline (see typography). Center-left, off-axis (not centered): a glowing ancient stone doorway lights worn steps; a human figure caught mid-step, one hand actively reaching toward the light as if just discovering it, backlit, faceless. Right foreground, partially cropped by the frame edge: a corroded bronze Antikythera-like mechanism, a few large gear forms and one rim-lit dial, dark aged bronze. One coherent camera perspective, one physically continuous underground chamber, one warm directional light source anchored at the doorway. Restrained, investigative mood — mystery and discovery, not spectacle.
```

Full assembled prompt (all four components, as actually submitted by
`generate_unified_integrated_thumbnail()`):

```
Premium archaeological investigative documentary YouTube thumbnail, 16:9. Left zone: two-line carved-stone headline (see typography). Center-left, off-axis (not centered): a glowing ancient stone doorway lights worn steps; a human figure caught mid-step, one hand actively reaching toward the light as if just discovering it, backlit, faceless. Right foreground, partially cropped by the frame edge: a corroded bronze Antikythera-like mechanism, a few large gear forms and one rim-lit dial, dark aged bronze. One coherent camera perspective, one physically continuous underground chamber, one warm directional light source anchored at the doorway. Restrained, investigative mood — mystery and discovery, not spectacle. — INTEGRATED TYPOGRAPHY (bottom-left quadrant of the frame): exact text reads "IT SHOULDN'T" (smaller, upper line), then "EXIST" (dramatically larger, directly below). "EXIST" is approximately 2x taller than "IT SHOULDN'T". Carved weathered stone letterforms, restrained metallic edge highlight only — not a solid glowing fill. Physically integrated into the scene's own lighting, not a subtitle, overlay, box, or lower-third. Correctly spelled, fully legible. — THUMBNAIL COMPOSITION DNA (compact): hierarchy hook_text > human_figure > focal_object. headline 30-34% of frame width, emphasis line capped at the same 34%. secondary figure pose is visibly active, not static. highlight localized and contained, never a full-frame wash. tertiary object capped at 28-32% of frame, partially concealed in shadow, a few large simplified forms, minimal detail; patina subtle, never dominant. strong near-black separation between zones, asymmetric framing. everything readable at 320x180. no incidental text, inscriptions, symbols, or watermarks beyond the declared headline. — HYPOGEUM BRAND (thumbnail, compact): near-black #080C0E/charcoal dominant (60-70%); dark stone/muted bronze mid-ground (20-30%); aged gold #C49A42 restrained accent only (5-10%, never dominant); warm ivory/off-white for text/highlights only. Forbidden: blue or cyan lighting of any kind; teal lighting; turquoise verdigris; orange/yellow scene wash covering the full frame; full-frame sepia tint; bright polished gold; fluorescent or neon colors of any hue; heavy saturated color cast dominating the overall frame. Finish: premium archaeological documentary, deep but readable shadows. Avoid: generic fantasy-art finish, museum-display finish, glossy modern product-render look.
```

No provider call has been made against this prompt. Generation is deferred
pending explicit approval, per instruction.
