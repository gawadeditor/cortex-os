# Brand Bible Extraction Audit

**Date:** 2026-06-06  
**Source image:** `docs/brand_guardian/bible.png`  
**Auditing:** `docs/brand_guardian/brand_bible_hypogeum.json` v1  
**Method:** Direct visual inspection of `bible.png`. Every claim below is traced to a specific observable element in the image.

**Confidence scale used throughout:**
- **HIGH** — Explicitly printed text or unambiguous visual element. No interpretation required.
- **MEDIUM** — Visually inferable from the image with reasonable certainty. Some interpretation involved.
- **LOW** — Weakly suggested by the image. Alternative interpretations plausible.
- **NOT IN IMAGE** — No visual evidence for this claim exists in `bible.png`. Derived from prior system design, schema conventions, or inference beyond what the image shows.

---

## Section 1 — Identity

### What is directly observed in bible.png

**Channel name and wordmark — HIGH**

The top-left quadrant of the image is occupied by a large primary wordmark treatment. Directly readable:

```
THE
HYPOGEUM
— CHRONICLES —
```

All three lines are clearly legible. The structural hierarchy is unambiguous: THE is visually subordinate, HYPOGEUM is the dominant element, CHRONICLES is intermediate in scale.

**Em-dashes flanking CHRONICLES — HIGH**

The em-dashes (`—`) flanking CHRONICLES are directly visible in the image. They are not hyphens — the dash length matches an em-dash. Both flanking marks are present and symmetrical.

**Tagline — HIGH**

Below the wordmark, the following text is directly readable: `UNEARTHING THE STORY / BENEATH OUR FEET`

The tagline appears in a smaller, lighter typeface, clearly subordinate to the wordmark.

**Small architectural icon — MEDIUM**

Between the HYPOGEUM and CHRONICLES lines there is a small graphical element. At image resolution it appears to be a columnar or temple-facade design, consistent with the classical_columns element in the graphic elements section. However, the exact form is not fully legible at this resolution. Cannot confirm every detail.

**Content pillars — HIGH**

The footer of the image, in a strip at the very bottom, shows:
`ARCHAEOLOGY — HISTORY — MYSTERY — DISCOVERY — HERITAGE — LEGACY`

A small compass rose or similar decorative element separates MYSTERY and DISCOVERY. All six words are directly readable.

### What was inferred (not directly labeled)

**`visual_personality`** — "Ancient authority. Archaeological gravitas. Measured mystery." — **INFERRED**. This is an editorial characterisation synthesised from the overall mood of the image. The image does not state this. It is a reasonable inference from the visual tone but it is interpretation, not extraction.

**`emotional_register`** (awe / curiosity / unease / reverence / discovery) — **INFERRED**. The image does not label emotional registers. These were derived from the Mood & Atmosphere panel imagery. Reasonable but not stated.

**`audience_expectation`** — "Serious historical content. The viewer expects depth, credibility..." — **NOT IN IMAGE**. Invented editorial copy.

**`visual_positioning`** — "Cinematic archaeology documentary. The visual register of prestige historical film." — **NOT IN IMAGE**. Invented editorial characterisation.

**`brand_promise`** — "Unearthing the story beneath our feet." — This is the tagline text rephrased as a brand promise declaration. The tagline is directly observed. The framing of it as a `brand_promise` field value is an interpretation.

### Confidence summary — Identity

| Field | Status | Confidence |
|---|---|---|
| `channel_name` | Directly observed | HIGH |
| `wordmark` | Directly observed | HIGH |
| `wordmark_structure` (3 lines + hierarchy) | Directly observed | HIGH |
| `wordmark_structure.line_3_treatment` (em-dashes) | Directly observed | HIGH |
| `tagline` | Directly observed | HIGH |
| `content_pillars` | Directly observed | HIGH |
| `visual_personality` | Inferred | MEDIUM |
| `emotional_register` | Inferred from mood board | LOW–MEDIUM |
| `audience_expectation` | Not in image | **REQUIRES HUMAN APPROVAL** |
| `visual_positioning` | Not in image | **REQUIRES HUMAN APPROVAL** |
| `brand_promise` | Tagline restated | MEDIUM |

---

## Section 2 — Color System

### What is directly observed in bible.png

**8 color swatches — HIGH (general), MEDIUM (exact hex)**

The COLOR PALETTE section in the bottom-left quadrant shows a row of 8 color swatches running from near-black to parchment cream. The swatches are clearly visible. Hex labels are printed below each swatch in small white text.

**Observable swatch colors (by inspection):**

| Position | Observable Color | Extracted Hex | Hex Confidence |
|---|---|---|---|
| 1 (leftmost) | Near-black with minimal warmth | `#080808` | MEDIUM — label partially legible, consistent with swatch |
| 2 | Very dark with slight warm-purple cast | `#1A1520` | MEDIUM — the warm-purple undertone is visible; exact digits uncertain |
| 3 | Dark warm brown-charcoal | `#2B2020` | MEDIUM — warm brownish dark; digits uncertain |
| 4 | Medium-dark warm brown | `#4C3A20` | MEDIUM — earthy brown, clearly distinct from positions 3 and 5 |
| 5 | Medium dark gold-brown (darker bronze) | `#7A5C14` | MEDIUM — clearly a muted bronze/dark gold |
| 6 | Warm amber-gold | `#B8882A` | MEDIUM — clearly gold/amber tone |
| 7 | Lighter warm gold | `#CBAA62` | MEDIUM — lighter gold, distinct from position 6 |
| 8 | Parchment cream (lightest) | `#F0E6C8` | MEDIUM — clearly a warm off-white/cream |

**Critical note:** The hex labels ARE printed in the image. However, at the rendered resolution of this image, the individual hex digits are not reliably legible beyond general confirmation that the values are plausible for the observed swatch color. The labels were used to anchor the initial extraction. They must be verified by eyedropping from the original source file before any hex value becomes binding.

**Color families are HIGH confidence. Exact hex digits are MEDIUM confidence at best.**

### What was inferred

**Swatch roles and usage assignments** — MEDIUM to LOW confidence. The role assigned to each swatch (e.g., "primary background", "secondary background / depth layer") was inferred from:
- How the colors appear in the main wordmark composition (left side of image)
- How the colors appear in the thumbnail examples
- General principles of dark-on-dark layering

These role assignments are reasonable but not stated in the image.

**Thumbnail palette rules** (allowed_range, forbidden_colors, contrast_minimum) — **INFERRED**. These rules were derived by examining what colors appear and do not appear across the thumbnail examples and visual style board. The image does not label these as rules.

**`text_on_dark` hierarchy** (warm_gold for display, parchment_cream for body) — **MEDIUM confidence**. Directly observable in the wordmark (HYPOGEUM appears in a gold tone, the tagline in a lighter cream), but the specific hex assignments to these roles require verification.

**`overlay_opacity_range: 0.7–0.92`** — **NOT IN IMAGE**. This value was invented. No opacity specification appears anywhere in `bible.png`.

**`text_on_light: "TBD — avoid light surfaces"`** — **NOT IN IMAGE**. Reasonable inference (no light surfaces appear in the reference), but it is an inference.

**`period_palette_constraints.global_forbidden_colors`** — **INFERRED** from absent colors. The reference contains no light surfaces, no cool blues, no neons, etc. The prohibition list was derived from what is absent rather than what is stated. This is valid inference but is not a stated rule.

### Confidence summary — Color System

| Field | Status | Confidence |
|---|---|---|
| 8 swatches exist | Directly observed | HIGH |
| General color of each swatch | Directly observed | HIGH |
| Exact hex digits | Partially legible from image labels | MEDIUM — verify required |
| Swatch role assignments | Inferred from composition | MEDIUM |
| `thumbnail_palette.forbidden_colors` | Inferred (absent colors) | LOW |
| `overlay_opacity_range` | Not in image | **REQUIRES HUMAN APPROVAL** |
| `text_on_light` | Not in image | **REQUIRES HUMAN APPROVAL** |

---

## Section 3 — Typography

### What is directly observed in bible.png

The TYPOGRAPHY section occupies the lower-centre of the image and contains two panels:

**Primary typeface — HIGH**

Left panel: A large display "Aa" in a classical Roman serif typeface. Below it, in clear printed text: **CINZEL SEMIBOLD**. A full alphabet specimen follows (uppercase, lowercase/small caps, numerals 0–9). The typeface is visually consistent with Cinzel: Roman inscriptional letterforms, no decorative serifs, classical proportions. The font name is directly and unambiguously printed.

**Secondary typeface — HIGH**

Right panel: A large display "Aa" in a geometric sans-serif typeface. Below it, in clear printed text: **RALEWAY REGULAR**. A full alphabet specimen follows. The typeface is visually consistent with Raleway: geometric construction, even stroke weight, no serif. The font name is directly and unambiguously printed.

**Evidence for identification:**

- Cinzel: The letterforms in the specimen are unambiguously a Roman inscriptional design. The A has a pointed apex, O is circular, the overall proportions match Cinzel. The label "CINZEL SEMIBOLD" is printed directly beneath the specimen.
- Raleway: The letterforms show a geometric sans design with slightly stylized R and Q characters (consistent with Raleway's distinctive glyphs). The label "RALEWAY REGULAR" is printed directly beneath the specimen.

**Both font names are the highest-confidence extraction in the entire Bible.**

### What was inferred

**`thumbnail_type` rules** (max_words: 4, max_characters: 30, tracking: "wide") — **INFERRED**. These were derived by counting words and estimating character counts in the 4 thumbnail examples. The image does not state these limits. The examples happen to be short, but that does not establish a maximum.

**`shorts_overlay_type`** — **NOT IN IMAGE / PRE-EXISTING**. The overlay structure rules were carried over from the existing Shorts spec, not extracted from bible.png. The font assignment (Raleway Regular) is a reasonable inference from the two-font system, but was not shown in a Shorts context in the reference.

**`nav_label_treatment`** — **MEDIUM**. The footer label treatment (ALL CAPS, Raleway, wide tracking, ornament separators) is directly observable in the footer strip of the image.

**`forbidden_typefaces` and `forbidden_treatments`** — **INFERRED**. The logical inverse of "only Cinzel and Raleway are permitted" produces the forbidden list. Specific named forbidden typefaces (Comic Sans, Impact, Arial, etc.) were added as examples and were **NOT IN IMAGE**.

**`thumbnail_type.max_words_headline: 4`** — This was counted from the thumbnail examples. However, 3 of the 4 thumbnails show short text. This is not a stated maximum — it is an observed range. These are different things.

### Confidence summary — Typography

| Field | Status | Confidence |
|---|---|---|
| Primary typeface name: Cinzel SemiBold | Directly printed in image | HIGH |
| Secondary typeface name: Raleway Regular | Directly printed in image | HIGH |
| Both fonts confirmed as serif + geometric sans pair | Directly observable | HIGH |
| `thumbnail_type.max_words` | Inferred from examples | LOW — observed range, not stated rule |
| `thumbnail_type.max_characters` | Inferred from examples | LOW — counted, not stated |
| `shorts_overlay_type` rules | Pre-existing spec, not from image | **REQUIRES HUMAN APPROVAL** |
| `forbidden_typefaces` (specific names) | Not in image | **REQUIRES HUMAN APPROVAL** |
| `forbidden_treatments` list | Inferred from absence | LOW |

---

## Section 4 — Thumbnail Language

### Separating observed facts from inferred rules

**Directly observed facts (HIGH confidence):**

1. There are exactly 4 thumbnail examples in the TITLE EXAMPLES section.
2. None of the 4 thumbnails contain human faces.
3. All 4 thumbnails use the full three-line wordmark hierarchy (THE / HYPOGEUM / — CHRONICLES —).
4. All 4 thumbnails use dark atmospheric backgrounds (stone, architectural imagery).
5. The wordmark is centred in all 4 examples.
6. In 3 of 4 thumbnails, there is additional text below the wordmark.
7. The thumbnail backgrounds are consistent with the visual style board imagery (stone, ruins, etc.).
8. Thumbnail 1 includes the tagline text ("UNEARTHING THE STORY BENEATH OUR FEET").
9. Thumbnail 3 includes a statement-format subtitle. The text appears to read "THE PAST IS NOT SILENT" — legible with MEDIUM confidence.
10. Thumbnail 2 includes what appears to be an episode number and subtitle — partially legible. "EPISODE II" is observable. The subtitle text is not fully legible at this resolution.
11. Thumbnail 4 has a different atmospheric background (appears to show stone surface/texture) with minimal or different subtitle treatment.
12. The wordmark text color appears warm gold/amber in all examples.
13. Aspect ratio of all thumbnails is consistent with 16:9.

**Inferred rules (MEDIUM or lower confidence):**

1. **"Wordmark-dominant composition model"** — MEDIUM. This is a characterisation of the observed pattern. The observation is that the wordmark is always present and always large. The rule stating it is "the primary compositional anchor" is an editorial characterisation of that observation.
2. **"Background imagery is context, not foreground subject"** — LOW. Derived from the observation that no backgrounds contain a dominant non-wordmark foreground subject. This is an inference from what is absent.
3. **`curiosity_gap_requirement`** — INFERRED editorial characterisation. Not stated in the image.
4. **`background_treatment: "slight vignette or darkening at edges"`** — LOW confidence. May be observable in some thumbnails but the image resolution makes this claim uncertain.
5. **`brightness_contrast_range: "0–40% brightness on background"`** — INVENTED. A quantification that has no stated basis in the image.
6. **`approval_threshold` wording** — INVENTED editorial copy.

### Confidence summary — Thumbnail Language

| Field | Status | Confidence |
|---|---|---|
| Exactly 4 thumbnail variants exist | Directly observed | HIGH |
| No human faces in any thumbnail | Directly observed | HIGH |
| All use full wordmark hierarchy | Directly observed | HIGH |
| Wordmark centred in all examples | Directly observed | HIGH |
| Dark atmospheric backgrounds | Directly observed | HIGH |
| Tagline variant exists | Directly observed | HIGH |
| Statement variant text ("THE PAST IS NOT SILENT") | Partially legible | MEDIUM |
| Episode subtitle variant with "EPISODE II" | Partially legible | MEDIUM |
| 4th variant subtitle text | Not fully legible at resolution | LOW |
| `composition_model` characterisation | Inferred from observation | MEDIUM |
| `brightness_contrast_range` (0–40%) | Not in image — invented | **REQUIRES HUMAN APPROVAL** |
| `curiosity_gap_requirement` | Not in image — inferred | **REQUIRES HUMAN APPROVAL** |
| `approval_threshold` wording | Not in image — invented | **REQUIRES HUMAN APPROVAL** |
| `face_rules.allowed: false` | Directly derived from 0/4 examples | HIGH |

---

## Section 5 — Documentary Visual Grammar

### What is directly observed in bible.png

The VISUAL STYLE section shows 6 photographs:
1. Underground chamber with dramatic vertical light shaft from ceiling
2. Stone columns/pillars with warm atmospheric lighting
3. Ancient stone relief carvings (inscribed surface)
4. Dramatic mountainous landscape, dark and moody
5. Antique cartographic map (overhead/oblique view)
6. Close-up stone carving detail

The MOOD & ATMOSPHERE section shows 3 photographs:
1. Dark arched stone corridor/tunnel
2. Ancient carved circular design on stone
3. Single human silhouette in a doorway/arch opening (small figure against large architectural context)

**What the images confirm:**
- Environmental and architectural imagery: directly observed
- Archaeological artefact and surface imagery: directly observed
- Single figure silhouette in architectural context: directly observed
- Dark, high-contrast lighting with single directional source: directly observed in most images

**What the images do NOT contain:**
- No talking heads
- No interview setups
- No visible lip sync
- No direct-to-camera address

### Critical note on the forbidden visual modes

The `documentary_visual_grammar.forbidden_visual_modes` list in the Bible is **NOT derived from bible.png**. It was established in the VPD system prompt **prior to** the bible.png extraction work. It was ported into the Bible schema because it represents an existing, active constraint.

The visual reference confirms these modes are absent from the reference imagery — but that confirms consistency, not origin. The rules predate the image.

**These rules were correctly placed in the Bible but their origin is the VPD system prompt, not the visual reference. They should be marked as pre-existing policy, not as extracted rules.**

### Lighting grammar — what is observed vs. invented

**Directly observed:**
- Single strong directional light source: visible in multiple style board images (shaft of light from above in image 1; warm side-lighting in image 2)
- High contrast, deep shadows: directly observable in most mood board images
- Warm amber and natural daylight character: directly observable

**Invented:**
- **"~70–90% of frame in shadow"** — specific quantification not stated in image. This was invented from visual inspection.
- **"Approximately 70-90% of frame in shadow on establishing shots"** — **NOT IN IMAGE. INVENTED.**

### Confidence summary — Documentary Visual Grammar

| Field | Status | Confidence |
|---|---|---|
| Environmental/architectural imagery is on-register | Directly observed | HIGH |
| Single figure silhouette in environment | Directly observed | HIGH |
| Underground chamber with light shaft | Directly observed | HIGH |
| Cartographic/map imagery | Directly observed | HIGH |
| No faces / no talking heads in reference | Directly observed | HIGH |
| Forbidden visual modes list | Pre-existing VPD rules — not from image | Pre-existing policy |
| `lighting_grammar` (directional source, high contrast, warm) | Directly observed | HIGH |
| `lighting_grammar.shadow_depth: "70–90%"` | Not in image — invented | **REQUIRES HUMAN APPROVAL** |
| `narration_model` | Not in image — pre-existing policy | Pre-existing policy |

---

## Section 6 — Motion Language

### What is directly observed in bible.png

**Nothing in `bible.png` shows motion.** The image is a static visual reference sheet. It contains still photographs.

The motion language section was derived entirely by inference from the static images:
- Slow pan: inferred from the atmospheric landscapes and chambers (these "feel" like they would be filmed slowly)
- Slow push-in: inferred from the detail shots of carvings
- Static with atmosphere: inferred from mood board images

**Every single motion type in the motion_language section is an inference from a static image.** None of them are stated, shown, or implied by any text in the reference.

The on-register and off-register motion type lists, the camera movement vocabulary, the scene-type motion preferences, and the motion speed register description are all editorial characterisations that have no direct evidence in the image.

**The motion_language section is almost entirely INFERRED or INVENTED.**

### Confidence summary — Motion Language

| Field | Status | Confidence |
|---|---|---|
| Any field in `motion_language` | Not in image — inferred from static imagery | LOW |
| All fields | Not stated in reference | **ALL REQUIRE HUMAN APPROVAL** |

---

## Section 7 — Logo System

### What is directly observed in bible.png

**Primary wordmark:**
- THE HYPOGEUM CHRONICLES in Cinzel SemiBold: HIGH (directly observed)
- Em-dash flanking rule: HIGH (directly observed)
- Primary color warm gold on dark background: HIGH (directly observable in main composition)

**Permitted placements:**
- Thumbnail centred: HIGH (observed in all 4 thumbnail examples)
- Other placements (end card, lower third): **NOT IN IMAGE** — no end card or lower third examples are shown.

**Color variants (warm_gold on void_black, etc.):** MEDIUM — only the primary (gold on black) is directly observable. The alternate variants were inferred.

**`minimum_size`, `clear_space`:** NOT IN IMAGE.

**`watermark_rules`, `end_card_rules`:** NOT IN IMAGE.

### Confidence summary — Logo System

| Field | Status | Confidence |
|---|---|---|
| Wordmark uses Cinzel SemiBold | Directly observed | HIGH |
| Em-dash rule | Directly observed | HIGH |
| Primary color: warm gold on dark | Directly observed | HIGH |
| Thumbnail: centred placement | Directly observed | HIGH |
| End card and lower third placements | Not in image | **REQUIRES HUMAN APPROVAL** |
| `color_variants_permitted` (non-primary) | Not in image — inferred | **REQUIRES HUMAN APPROVAL** |
| `forbidden_treatments` list | Partially inferred | LOW–MEDIUM |
| `minimum_size`, `clear_space` | Not in image | **REQUIRES HUMAN APPROVAL** |
| `watermark_rules`, `end_card_rules` | Not in image | **REQUIRES HUMAN APPROVAL** |

---

## Section 8 — Iconography

### What is directly observed in bible.png

The GRAPHIC ELEMENTS section shows 5 elements clearly. They are individually identifiable:

1. **Compass rose** — Multi-point radiating star/wind-rose design. Observable: HIGH
2. **Ouroboros** — Circular snake consuming its own tail. Observable: HIGH  
3. **Classical columns** — Two columns with capital detail. Observable: HIGH
4. **Drafting compass/dividers** — V-shaped precision instrument. Observable: HIGH
5. **Circle medallion** — Simple circle outline. Observable: HIGH

**All 5 elements appear in warm gold line art style on a dark background.** HIGH confidence.

**The line art rendering style (not filled) is directly observable.** HIGH confidence.

**Symbolic meaning assignments** (compass rose = "navigation", ouroboros = "cyclicality/hidden knowledge") — **INFERRED**. These are standard art-historical interpretations of these symbols, not stated in the image.

**Usage assignments** (compass rose on chapter cards, ouroboros on end cards, etc.) — **NOT IN IMAGE. INVENTED.** No usage context is shown for any element in the reference.

### Confidence summary — Iconography

| Field | Status | Confidence |
|---|---|---|
| 5 specific graphic elements confirmed | Directly observed | HIGH |
| Fine line art style (not filled) | Directly observed | HIGH |
| Warm gold color | Directly observed | HIGH |
| Dark background context | Directly observed | HIGH |
| Symbolic meanings | Inferred (art-historical convention) | MEDIUM |
| Usage assignments per element | Not in image | **REQUIRES HUMAN APPROVAL** |
| `preferred_symbolic_vocabulary` categories | Inferred from element types | MEDIUM |
| `forbidden_symbolic_vocabulary` | Not in image — inferred | LOW |

---

## Section 8.5 — Texture System (added post-audit)

### What is directly observed in bible.png

The TEXTURES section of `bible.png` is in the bottom-right quadrant. It shows **6 square swatches arranged in a 2×3 grid**, running from dark (top-left) to light (bottom-right). The section is labelled "TEXTURES".

This section was omitted from the initial extraction and has been added as a result of this audit.

**Directly observed:**
- 6 texture swatches exist: HIGH confidence
- Section label "TEXTURES" is printed: HIGH confidence
- All swatches fall within the warm earth tone palette (no cool or saturated tones): HIGH confidence
- Progression from near-black (top-left) to parchment cream (bottom-right): HIGH confidence, matches the color palette sequence
- Top-right swatch (parchment_aged_paper) is HIGH confidence — clearly the lightest, warm cream
- Top-left swatch (black_stone_obsidian) is HIGH confidence — clearly the darkest

**MEDIUM confidence (category names inferred from visual appearance):**
- carved_stone_relief: surface shows textural markings consistent with carved stone
- archaeological_surface_detail: worn stone surface with aged quality
- dark_map_line_texture: fine linear detail, consistent with map or document texture
- cracked_earth_aged_plaster: cracking or desiccation pattern visible

**What the image does NOT contain:**
- No labels for individual swatch names
- No usage assignments (which texture applies where)
- No opacity or layering instructions
- No specification of whether textures are mandatory or decorative

### Confidence summary — Texture System

| Field | Status | Confidence |
|---|---|---|
| 6 texture swatches exist | Directly observed | HIGH |
| Palette consistency (warm earth tones) | Directly observed | HIGH |
| black_stone_obsidian category | Directly observed | HIGH |
| parchment_aged_paper category | Directly observed | HIGH |
| carved_stone_relief, archaeological, map, cracked categories | Inferred from visual appearance | MEDIUM |
| Usage assignments | Not in image | **REQUIRES HUMAN APPROVAL** |
| Opacity / layering rules | Not in image | **REQUIRES HUMAN APPROVAL** |
| Mandatory vs decorative status | Not in image | **REQUIRES HUMAN APPROVAL** |

---

## Section 9 — Forbidden Patterns

### Origin audit for each pattern category

**`visual_patterns`** (talking heads, lip sync, interview framing, etc.)
- Origin: **Pre-existing VPD system prompt rules**, ported into the Bible.
- Confirmed absent in the reference: YES — no such content appears in bible.png.
- Source category: **Pre-existing policy**.

**`composition_patterns`**
- "Human faces as primary thumbnail subject" — INFERRED from 0/4 thumbnail examples having faces. MEDIUM.
- "Bright high-key backgrounds" — INFERRED from observed dark backgrounds. MEDIUM.
- "Layouts that destroy wordmark hierarchy" — INFERRED from consistent wordmark hierarchy in examples. MEDIUM.
- "Compositions that minimise the wordmark" — INFERRED. LOW.

**`content_patterns`** (fabricated footage, synthesized voice, disputed interpretation)
- Origin: **Pre-existing channel policy**, not derived from bible.png.
- Source category: **Pre-existing policy**.

**`thumbnail_patterns`**
- "Human faces" — INFERRED from examples. HIGH by observation.
- "Bright/saturated color palette" — INFERRED from absent colors. MEDIUM.
- "Non-Cinzel/Raleway typography" — INFERRED from font system. MEDIUM.
- "Missing em-dash decorators" — Directly observed rule. HIGH.
- Other items: INFERRED.

**`typography_patterns`**
- "Any typeface outside Cinzel/Raleway" — Directly derived from type system. HIGH.
- Bold Raleway in overlays — **NOT IN IMAGE**. Extrapolation from font weight specification.
- Drop shadows, outlined strokes, gradient fills — **NOT IN IMAGE**. Invented list of common prohibitions.

**`colour_patterns`**
- Bright white, neon, cool blues: INFERRED from absent colors. MEDIUM.
- "Bright oranges outside burnished_bronze range" — LOW confidence. Subjective boundary.

**`motion_patterns`**
- All items: **INFERRED from static images**. Not stated. LOW.

**`atmosphere_patterns`**
- All items: **INFERRED from mood board imagery**. Not stated. LOW.

---

## Section 10 — Social Media Rules

### What is directly observed in bible.png

**YouTube thumbnails:** The 4 thumbnail examples provide evidence for thumbnail treatment. HIGH for what is shown.

**Nothing else in social_media_rules has evidence in bible.png.** No Instagram formats, no TikTok formats, no end card layouts, no chapter card layouts are shown.

**The YouTube section beyond thumbnail rules is NOT IN IMAGE.**

**Instagram and TikTok sections are entirely NOT IN IMAGE.**

---

## Section 11 — Shorts Rules

### What is directly observed in bible.png

**Nothing.** No Shorts format content is shown in the visual reference. The Shorts rules section was ported from the pre-existing Shorts specification.

The typography assignment (Raleway Regular) is a reasonable inference from the two-font system but has no direct Shorts-context evidence.

**Source category: Pre-existing policy.**

---

## Section 12 — Publishing Rules

### What is directly observed in bible.png

**Nothing.** No publishing metadata rules, description structures, title formats, tag rules, or AI disclosure requirements are stated or implied in `bible.png`.

**The entire publishing_rules section is pre-existing channel policy, not visual reference extraction.**

---

## Textures Section — Omission Note

The TEXTURES section of bible.png shows 6 stone/material texture swatches in warm earth tones. This section was **not extracted into the Brand Bible JSON**. These textures appear to be reference material for visual style consistency (stone surfaces, carved surfaces, etc.) and may inform background treatment specifications. This is a gap — the textures were observed but not captured.

---

## Final Summary

### Directly Extracted Rules (HIGH confidence, from bible.png)

These are facts observable directly in the image with minimal interpretation:

1. Channel name: THE HYPOGEUM CHRONICLES
2. Tagline: UNEARTHING THE STORY BENEATH OUR FEET
3. Wordmark: three-line hierarchy THE / HYPOGEUM / — CHRONICLES —
4. Em-dash flanking CHRONICLES is mandatory (directly visible)
5. Primary typeface: Cinzel SemiBold (printed)
6. Secondary typeface: Raleway Regular (printed)
7. 8 color swatches run from near-black to parchment cream (observed — exact hex digits require verification)
8. General color character: warm, dark, earth/gold tones throughout. No cool, neon, or saturated tones present anywhere in the reference.
9. Warm gold line art style for all 5 graphic elements (directly observed)
10. 5 confirmed graphic elements: compass rose, ouroboros, classical columns, drafting compass, circle medallion
11. Content pillars: ARCHAEOLOGY — HISTORY — MYSTERY — DISCOVERY — HERITAGE — LEGACY (directly printed)
12. Thumbnail wordmark-dominant composition (directly observed across all 4 examples)
13. No human faces in any of the 4 thumbnail examples (directly observable)
14. All thumbnails use dark atmospheric backgrounds (directly observable)
15. 4 thumbnail variants exist (tagline, episode subtitle, statement, minimal/atmospheric)
16. Visual style imagery types: underground chambers, stone carvings, maps, mountain landscapes, arched corridors (directly observed in style board)
17. Mood board confirms silhouette-in-environment as on-register imagery (directly observed)
18. Lighting in visual style images: single directional source, high contrast, warm/natural character (directly observable)
19. No light surfaces appear anywhere in the reference (directly observable)
20. Footer nav treatment: Raleway ALL CAPS wide tracking with ornament separator (directly observable)
21. TEXTURES panel exists with 6 swatches (directly observed)
22. All texture swatches are warm earth tones — palette-consistent, no cool or synthetic materials (directly observed)
23. Two HIGH-confidence texture categories: black_stone_obsidian (darkest) and parchment_aged_paper (lightest)

---

### Inferred Rules (MEDIUM confidence — derived from image evidence, but not stated)

These require human review before becoming binding:

1. Swatch role assignments (which swatch is "primary background", etc.) — inferred from usage in composition
2. Forbidden color families (bright white, neon, cool blues, etc.) — inferred from what is absent
3. Thumbnail composition rules beyond the directly observable — inferred from the 4 examples
4. Color of display text: warm gold for headings, cream for body — inferred from wordmark rendering
5. Symbolic meaning assignments for graphic elements — standard art-historical interpretation
6. Symbolic vocabulary categories (navigation, archaeology, etc.) — inferred from element types
7. `visual_personality` description — synthesised editorial characterisation of observable tone
8. `emotional_register` list — inferred from mood board imagery

---

### Pre-Existing Policy (not from bible.png — correct to include but source must be noted)

These rules are valid but were NOT derived from the visual reference. Their origin is the VPD system prompt or prior channel policy decisions:

1. `documentary_visual_grammar.forbidden_visual_modes` — from VPD system prompt
2. `documentary_visual_grammar.narration_model` — from VPD system prompt
3. `documentary_visual_grammar.person_speaking_substitution_rule` — from VPD system prompt
4. `publishing_rules.ai_disclosure.required: true` — from channel policy
5. `publishing_rules.description_rules.required_sections` — from channel parameters
6. `shorts_rules.overlay_structure` — from pre-existing Shorts spec
7. `shorts_rules.max_words_per_overlay: 7` — from pre-existing Shorts spec
8. `shorts_rules.redirect_text_rule` — from pre-existing Shorts spec
9. `forbidden_patterns.content_patterns` — from channel policy
10. `forbidden_patterns.visual_patterns` — from VPD system prompt

---

### Requires Human Approval (invented or low-evidence items currently in the Bible)

These items exist in `brand_bible_hypogeum.json` but have no or insufficient visual evidence. They must not become binding BGA rules until a human explicitly approves each one:

| Item | Current location in Bible | Issue |
|---|---|---|
| `color_system.overlay_opacity_range: 0.7–0.92` | `color_system` | Invented. No basis in image. |
| `documentary_visual_grammar.lighting_grammar.shadow_depth: "70–90%"` | `documentary_visual_grammar` | Invented quantification. |
| `thumbnail_type.max_words_headline: 4` | `typography` | Counted from examples. Not a stated rule. |
| `thumbnail_type.max_characters_headline: 30` | `typography` | Counted from examples. Not a stated rule. |
| `thumbnail_language.brightness_contrast_range: "0–40% brightness"` | `thumbnail_language` | Invented quantification. |
| `thumbnail_language.curiosity_gap_requirement` | `thumbnail_language` | Not in image. Editorial inference. |
| `thumbnail_language.approval_threshold` wording | `thumbnail_language` | Invented editorial copy. |
| `identity.audience_expectation` | `identity` | Invented. Not in image. |
| `identity.visual_positioning` | `identity` | Invented editorial characterisation. |
| All fields in `motion_language` | `motion_language` | Entirely inferred from static images. |
| `logo_system.permitted_placements` (end card, lower third) | `logo_system` | Not shown in image. |
| `logo_system.color_variants_permitted` (non-primary) | `logo_system` | Not shown in image. |
| `logo_system.forbidden_treatments` list | `logo_system` | Inferred, not stated. |
| `iconography.usage` per element | `iconography` | Not in image. Invented. |
| `iconography.forbidden_symbolic_vocabulary` | `iconography` | Not in image. Inferred inverse. |
| `typography.forbidden_treatments` list | `typography` | Not in image. Common-sense list. |
| `typography.forbidden_typefaces` specific names | `typography` | Not in image. Examples added editorially. |
| `shorts_rules` all typography/visual fields | `shorts_rules` | Not in image. Inferred from font system. |
| All `publishing_rules` | `publishing_rules` | Not in image. Pre-existing policy requires separate approval. |
| All `social_media_rules` beyond YouTube thumbnail treatment | `social_media_rules` | Not in image. |
| `forbidden_patterns.motion_patterns` | `forbidden_patterns` | Inferred from static images. |
| `forbidden_patterns.atmosphere_patterns` | `forbidden_patterns` | Inferred from mood board. |
| `forbidden_patterns.colour_patterns` specific items | `forbidden_patterns` | Inferred from absent colors. |
| `forbidden_patterns.typography_patterns` (beyond font system) | `forbidden_patterns` | Common-sense additions. |

---

### Missing Rules (present in image but not fully captured)

1. ~~**Textures**~~ — **Resolved.** `texture_system` section added to Bible JSON and this document (Section 8.5). Usage rules remain REQUIRES_HUMAN_APPROVAL.
2. **Icon between HYPOGEUM and CHRONICLES** — The small architectural icon in the wordmark is visible but not precisely described. Its exact form is unclear at this resolution. Captured as "columnar/temple icon" but exact design is unverified.
3. **Thumbnail 2 and 4 subtitle text** — The exact subtitle text in two of the four thumbnail examples was not fully legible at this resolution. Thumbnail 2 shows "EPISODE II" + subtitle (subtitle text unclear). Thumbnail 4 subtitle treatment is unclear.
4. **Ornament separator type in footer** — A small graphic element separates MYSTERY and DISCOVERY in the footer. It appears to be a compass rose or similar mark, consistent with the compass_rose graphic element. Exact form not captured as a rule.

### Update Log

- **2026-06-06 (post-audit):** `texture_system` section added to Bible. Textures moved from "missing" to "directly extracted." Extraction audit accepted by channel owner. User decisions on binding status recorded.

---

## Recommended Action on the Bible JSON

Based on this audit, the following changes to `brand_bible_hypogeum.json` are recommended before any section becomes binding:

1. **Mark `_binding: false`** on `motion_language` until a human explicitly defines the motion vocabulary.
2. **Remove or mark advisory** the `overlay_opacity_range` value — no image basis.
3. **Remove or mark advisory** the `lighting_grammar.shadow_depth` quantification.
4. **Remove or mark advisory** the `thumbnail_type.max_words` and `max_characters` limits, or change them from binding rules to observed-range notes.
5. **Change `thumbnail_language.brightness_contrast_range`** from a binding rule to advisory.
6. **Add `_source` field** to `documentary_visual_grammar.forbidden_visual_modes` noting origin as VPD system prompt.
7. **Add `_source: "pre-existing-policy"`** to all publishing_rules, shorts_rules overlay structure, and content_patterns fields.
8. **Hex values**: all currently at MEDIUM confidence. Require eyedrop verification from original source file before activation.
