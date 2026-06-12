# Brand Executable Specification — The Hypogeum Chronicles

**Version:** 1.2  
**Status:** Enforcement pass complete. ENFORCEABLE_CORE defined. Color hex values VERIFIED from approved Style Board (2026-06-08). Palette collapsed from 8 inferred swatches to 7 verified swatches.  
**Source:** `docs/brand_guardian/bible.png` — direct visual extraction  
**Produced:** 2026-06-06  

---

## Tag Definitions

| Tag | Meaning |
|---|---|
| `DIRECTLY_OBSERVED` | Text or visual element legible/unambiguous in bible.png without interpretation |
| `INFERRED` | Derived from observation but involves interpretation — not stated in image |
| `PRE_EXISTING_POLICY` | From VPD system prompt or prior channel policy — not from bible.png |
| `NOT_IN_IMAGE` | No visual evidence in bible.png — requires human specification |
| `NOT_VERIFIED` | Present in image but cannot be extracted to HIGH confidence at available resolution |

**Enforcement rule:** BGA may only enforce rules in `ENFORCEABLE_CORE`. BGA must not reject or flag content based on any `INFERRED`, `NOT_VERIFIED`, `NOT_IN_IMAGE`, or `PRE_EXISTING_POLICY` value that has not been explicitly activated.

---

## ENFORCEABLE_CORE

This section contains only rules that are:
- `DIRECTLY_OBSERVED` in bible.png
- HIGH confidence extraction
- Safe for automated enforcement without human interpretation at point of use

BGA enforcement is scoped to this section only until remaining fields are verified and approved.

### ENFORCEABLE: Typography

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-T01 | Primary typeface is **Cinzel SemiBold** | HIGH | Font name "CINZEL SEMIBOLD" is printed directly beneath the Aa specimen in the TYPOGRAPHY panel |
| E-T02 | Secondary typeface is **Raleway Regular** | HIGH | Font name "RALEWAY REGULAR" is printed directly beneath the Aa specimen in the TYPOGRAPHY panel |
| E-T03 | Exactly two typefaces are permitted. No typeface outside Cinzel or Raleway may appear in any output. | HIGH | Only two font specimens appear in the TYPOGRAPHY panel. The entire reference uses only these two fonts. |
| E-T04 | Wordmark line 1 ("THE") uses Cinzel SemiBold | HIGH | Directly visible in main wordmark composition and in all 4 thumbnail examples |
| E-T05 | Wordmark line 2 ("HYPOGEUM") uses Cinzel SemiBold | HIGH | Directly visible in main wordmark and all thumbnails |
| E-T06 | Wordmark line 3 ("— CHRONICLES —") uses Cinzel SemiBold | HIGH | Directly visible in main wordmark and all thumbnails |
| E-T07 | Tagline uses Raleway Regular | HIGH | Directly visible beneath wordmark: "UNEARTHING THE STORY BENEATH OUR FEET" |
| E-T08 | Footer / nav category labels use Raleway Regular in ALL CAPS with wide tracking | HIGH | Directly visible in footer strip: ARCHAEOLOGY — HISTORY — MYSTERY — DISCOVERY — HERITAGE — LEGACY |
| E-T09 | Cinzel renders as ALL CAPS and small caps only. No true lowercase. | HIGH | Typeface property of Cinzel — confirmed by specimen visible in image |
| E-T10 | All Cinzel display text is ALL CAPS in every observed instance | HIGH | Consistent across main wordmark, all 4 thumbnail wordmarks |

### ENFORCEABLE: Wordmark and Logo

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-L01 | The wordmark is a three-line hierarchy: **THE** / **HYPOGEUM** / **— CHRONICLES —** | HIGH | Directly visible. Three distinct lines with clear scale differentiation. |
| E-L02 | HYPOGEUM is the dominant element — largest scale in all wordmark instances | HIGH | Visible in main composition and all 4 thumbnails without exception |
| E-L03 | THE is subordinate — smaller scale than HYPOGEUM and CHRONICLES | HIGH | Directly visible in all wordmark instances |
| E-L04 | Em-dashes (—) flank CHRONICLES on both sides. This is mandatory. Hyphens are not a substitute. | HIGH | Em-dashes are directly visible in every wordmark instance. Never absent. |
| E-L05 | All wordmark lines use Cinzel SemiBold | HIGH | Consistent with E-T01; observed in all instances |
| E-L06 | Wordmark color is warm gold on dark backgrounds | HIGH | Directly observable in main composition and all 4 thumbnails |
| E-L07 | Wordmark placement in thumbnails: horizontally centred | HIGH | Consistent across all 4 thumbnail examples |
| E-L08 | A small architectural/columnar icon appears between HYPOGEUM and CHRONICLES in full wordmark treatment | HIGH (existence) / MEDIUM (exact form) | Visible in main composition — form is architectural/columnar but not fully legible at this resolution |
| E-L09 | CHRONICLES must not be scaled larger than HYPOGEUM | HIGH | In every observed instance HYPOGEUM is the largest element. This hierarchy is never violated. |

### ENFORCEABLE: Thumbnail Composition

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-TH01 | No human faces in thumbnails | HIGH | 0 of 4 thumbnail examples contain human faces. Directly countable. |
| E-TH02 | Background is a dark atmospheric image, full bleed | HIGH | Consistent across all 4 thumbnails |
| E-TH03 | Background subject matter is: stone, ruins, underground architecture, environmental landscape, cartographic material | HIGH | All 4 thumbnail backgrounds show this category of imagery. No exceptions. |
| E-TH04 | Wordmark is the compositionally dominant element — it is not subordinated to background imagery | HIGH | In all 4 examples the wordmark is the primary foreground element |
| E-TH05 | When subtitle text is present, it appears below the wordmark and is smaller than HYPOGEUM | HIGH | Observed in thumbnails 1, 2, 3 |
| E-TH06 | Four distinct thumbnail variant types exist | HIGH | 4 examples directly visible in TITLE EXAMPLES panel |
| E-TH07 | No light or bright-key backgrounds in thumbnails | HIGH | All 4 examples use dark backgrounds. Zero exceptions. |

### ENFORCEABLE: Color Character (palette direction, not specific hex)

Specific hex values are NOT_VERIFIED and are not in ENFORCEABLE_CORE. The palette character rules below are HIGH confidence because they describe the observable direction of the entire palette — not specific color values.

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-C01 | The palette contains 7 swatches running from near-black to parchment cream | HIGH | 7 verified swatches from approved Style Board (2026-06-08); original image extraction read 8 — one mid-tone gold swatch was a resolution artefact |
| E-C02 | All palette colors are warm earth tones — no cool, neon, or saturated colors | HIGH | Observable across the entire reference image without exception |
| E-C03 | No bright white (#FFFFFF or near-white) appears anywhere in the reference | HIGH | Consistently absent across entire image |
| E-C04 | No neon hues of any kind appear in the reference | HIGH | Consistently absent across entire image |
| E-C05 | No cool blues appear in the reference | HIGH | Consistently absent |
| E-C06 | No greens appear in the reference | HIGH | Consistently absent |
| E-C07 | No pinks or purples appear in the reference | HIGH | Consistently absent |
| E-C08 | No light surfaces appear in any composition context in the reference | HIGH | Consistently absent |
| E-C09 | Display typography color is warm gold (character: warm, gold-toned, not yellow, not orange) | HIGH | Wordmark text color directly observable. Specific hex NOT_VERIFIED. |
| E-C10 | Graphic element color is warm gold line art (same character as E-C09) | HIGH | All 5 graphic elements visible in warm gold |

### ENFORCEABLE: Graphic Elements

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-A01 | Five graphic elements exist: compass_rose, ouroboros, classical_columns, drafting_compass, circle_medallion | HIGH | All 5 directly identifiable in GRAPHIC ELEMENTS panel |
| E-A02 | All graphic elements are rendered as fine line art — no solid fills | HIGH | Directly observable. No element has a filled body. |
| E-A03 | All graphic elements are warm gold color on dark background | HIGH | Consistent across all 5 elements in the panel |
| E-A04 | compass_rose is used as a navigation separator in the footer | HIGH | Directly visible between content pillars in footer strip |
| E-A05 | classical_columns element appears as the small wordmark icon | HIGH (existence of icon) — MEDIUM (confirmed as columns specifically) | Architectural/columnar form visible between wordmark lines |

### ENFORCEABLE: Texture Vocabulary

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-X01 | Six texture categories exist in the visual vocabulary | HIGH | 6 swatches directly countable in TEXTURES panel |
| E-X02 | All textures are warm earth tones — no cool or synthetic materials | HIGH | Observable across all 6 swatches |
| E-X03 | Texture vocabulary is archaeological and historical in character | HIGH | Observable — stone, carved, parchment, earth categories |
| E-X04 | The two endpoint textures are: darkest = near-black stone; lightest = warm cream/parchment | HIGH | Top-left (darkest) and bottom-right (lightest) directly observable |

### ENFORCEABLE: Content Pillars

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-P01 | Six content pillars: ARCHAEOLOGY — HISTORY — MYSTERY — DISCOVERY — HERITAGE — LEGACY | HIGH | Directly printed in footer strip of bible.png. All 6 words legible. |

### ENFORCEABLE: Documentary Visual Character

These rules describe what is and is not present in the visual reference — not what should or should not be produced. They become enforceable VG rules when BGA validates visual plans against this character.

| Rule ID | Rule | Confidence | Evidence |
|---|---|---|---|
| E-VG01 | On-register imagery: environmental, architectural, artefact detail, underground chamber, cartographic | HIGH | Directly visible in VISUAL STYLE panel (6 images) and MOOD & ATMOSPHERE panel (3 images) |
| E-VG02 | Single human silhouette in environmental/architectural context is on-register | HIGH | Directly visible in mood board — small figure against large arch |
| E-VG03 | Lighting character: single strong directional source, high contrast, deep shadow | HIGH | Observable in multiple VISUAL STYLE images (light shaft from above, warm side-lighting) |
| E-VG04 | Lighting character: warm amber or natural daylight — not flat studio lighting | HIGH | Observable in visual style board images |

---

## Color System — Full Extraction (including NOT_VERIFIED)

### Palette Verification Statement

Hex values were verified 2026-06-08 from the approved Style Board provided by the channel owner. All 7 values are human-approved and canonical.

**Status:** All 7 hex values are `VERIFIED`. The initial image extraction produced 8 values, all marked `NOT_VERIFIED`. The approved Style Board collapsed the palette to 7 swatches (one mid-tone gold removed). This is the authoritative palette.

**What this means for enforcement:** BGA does not enforce specific hex values — enforcement remains character-based (warm earth tones, no cool/neon/bright). The verified hex values are now the canonical reference for any future tooling or documentation that references color values by code.

### Color Palette Table

Palette verified 2026-06-08 from approved Style Board. All 7 values are human-approved and treated as canonical. The previously extracted 8-value palette has been retired — one mid-tone gold swatch (`burnished_bronze #7A5C14`) was a resolution artefact from image extraction and is not present in the approved Style Board.

| Swatch | Name | Hex | RGB | Visual Character | Confidence | Enforceable |
|---|---|---|---|---|---|---|
| 1 (darkest) | void_black | `#080C0E` | `rgb(8, 12, 14)` | Near-pure black, very slight cool-dark undertone | VERIFIED | **YES** |
| 2 | deep_stone | `#1A1D20` | `rgb(26, 29, 32)` | Very dark, near-neutral dark stone | VERIFIED | **YES** |
| 3 | charcoal_earth | `#2B2E33` | `rgb(43, 46, 51)` | Dark charcoal with slight warmth | VERIFIED | **YES** |
| 4 | ancient_soil | `#5C4A2F` | `rgb(92, 74, 47)` | Medium-dark warm earthy brown | VERIFIED | **YES** |
| 5 | antique_gold | `#C49A42` | `rgb(196, 154, 66)` | Warm amber-gold | VERIFIED | **YES** |
| 6 | warm_gold | `#E7D6B3` | `rgb(231, 214, 179)` | Lighter warm gold / aged paper | VERIFIED | **YES** |
| 7 (lightest) | parchment_cream | `#F5F3EE` | `rgb(245, 243, 238)` | Warm cream / aged parchment | VERIFIED | **YES** |

### Color Combination Rules

Combination rules based on color character (not hex) are enforceable. Combination rules that require specific hex comparison are not.

| Rule | Basis | Enforceable |
|---|---|---|
| Dark background + warm gold text: ALLOWED | DIRECTLY_OBSERVED in all wordmark and thumbnail instances | **YES** (character-based) |
| Dark background + warm gold line art: ALLOWED | DIRECTLY_OBSERVED in graphic elements panel | **YES** (character-based) |
| Light background + any text: FORBIDDEN | No light background appears in any output context | **YES** (character-based — reject any light background) |
| Bright white as any surface: FORBIDDEN | Absent from entire reference | **YES** |
| Neon hue as any element: FORBIDDEN | Absent from entire reference | **YES** |
| Cool blue as any element: FORBIDDEN | Absent from entire reference | **YES** |
| Specific hex combination A + B: ALLOWED/FORBIDDEN | Requires verified hex values | **NO — until hex verified** |

---

## Typography — Full Specification

### Font Identification

| Property | Cinzel | Raleway |
|---|---|---|
| Confirmed name | Cinzel | Raleway |
| Confirmed weight | SemiBold | Regular |
| Full label in image | "CINZEL SEMIBOLD" | "RALEWAY REGULAR" |
| Extraction confidence | HIGH | HIGH |
| Enforceable | YES | YES |
| CSS font-weight | 600 | 400 |
| Classification | Classical Roman inscriptional serif | Geometric sans-serif |
| Source | Google Fonts | Google Fonts |
| URL | `https://fonts.google.com/specimen/Cinzel` | `https://fonts.google.com/specimen/Raleway` |
| URL source | Known Google Fonts reference — not printed in bible.png | Known Google Fonts reference — not printed in bible.png |

### Weight Availability vs. Permitted Weights

The image shows one weight per font. Other available weights are not shown, not addressed, and not prohibited by name in the image.

| Font | Weight shown | Other available weights | Status of other weights |
|---|---|---|---|
| Cinzel | SemiBold (600) — shown | Regular (400), Bold (700) | NOT_IN_IMAGE — requires human decision |
| Raleway | Regular (400) — shown | Thin (100) through ExtraBold (800), Italic variants | NOT_IN_IMAGE — requires human decision |

**Enforcement implication:** BGA can enforce "must use Cinzel or Raleway" (E-T03). BGA cannot enforce "must be SemiBold specifically" or "Cinzel Regular is forbidden" until a human explicitly approves those weight rules. The image proves the system has two fonts. It does not prove which weights of those fonts are forbidden.

### Usage Matrix (enforceable uses only)

| Context | Font | Weight | Case | Tag | Enforceable |
|---|---|---|---|---|---|
| Wordmark: all three lines | Cinzel | SemiBold | ALL CAPS | DIRECTLY_OBSERVED | YES |
| Tagline | Raleway | Regular | ALL CAPS, wide tracking | DIRECTLY_OBSERVED | YES |
| Footer category labels | Raleway | Regular | ALL CAPS, wide tracking | DIRECTLY_OBSERVED | YES |
| Thumbnail headline (wordmark) | Cinzel | SemiBold | ALL CAPS | DIRECTLY_OBSERVED | YES |
| Thumbnail subtitle | Raleway | Regular | ALL CAPS or mixed | DIRECTLY_OBSERVED MEDIUM | YES |
| Shorts overlays | Raleway | Regular | TBD | INFERRED from font system | NO — not shown in Shorts context |

---

## Logo System

| Specification | Value | Confidence | Enforceable |
|---|---|---|---|
| Three-line wordmark structure | THE / HYPOGEUM / — CHRONICLES — | HIGH | YES |
| HYPOGEUM is dominant (largest) | Observed in all instances | HIGH | YES |
| Em-dash flanking CHRONICLES | Mandatory — never absent | HIGH | YES |
| Wordmark typeface | Cinzel SemiBold | HIGH | YES |
| Primary color: warm gold on dark | Directly observed | HIGH | YES (character; not hex-specific) |
| Thumbnail placement: horizontally centred | Consistent across 4 examples | HIGH | YES |
| End card placement | Not in image | NOT_IN_IMAGE | NO |
| Video / lower third placement | Not in image | NOT_IN_IMAGE | NO |
| Shorts placement | Not in image | NOT_IN_IMAGE | NO |
| Minimum size | Not in image | NOT_IN_IMAGE | NO |
| Clear space | Not in image | NOT_IN_IMAGE | NO |
| Safe area margin | Not in image | NOT_IN_IMAGE | NO |

---

## Thumbnail System

| Specification | Value | Confidence | Enforceable |
|---|---|---|---|
| No human faces | 0/4 examples have faces | HIGH | YES |
| Dark atmospheric background | Consistent across all 4 | HIGH | YES |
| Wordmark compositionally dominant | Consistent across all 4 | HIGH | YES |
| Background: stone/ruins/architectural/cartographic | Consistent across all 4 | HIGH | YES |
| No light or bright backgrounds | Consistent across all 4 | HIGH | YES |
| Wordmark: horizontally centred | Consistent across all 4 | HIGH | YES |
| Subtitle below wordmark, subordinate scale | Thumbnails 1–3 | HIGH | YES |
| 4 variant types | Counted in panel | HIGH | YES |
| Maximum text density (word count) | Not stated | NOT_IN_IMAGE | NO |
| Numeric position rules (% from top) | Not stated | NOT_IN_IMAGE | NO |
| Minimum contrast ratio | Not stated | NOT_IN_IMAGE | NO |
| Brightness range of background (numeric) | Not stated | NOT_IN_IMAGE | NO |

---

## Shorts System

| Specification | Value | Source | Enforceable |
|---|---|---|---|
| Aspect ratio 9:16 | Pre-existing spec | PRE_EXISTING_POLICY | Conditionally YES — via Shorts spec, not BGA |
| max_words_per_overlay: 7 | Pre-existing spec | PRE_EXISTING_POLICY | Conditionally YES |
| Three required overlay beats | Pre-existing spec | PRE_EXISTING_POLICY | Conditionally YES |
| Redirect text rule | Pre-existing spec | PRE_EXISTING_POLICY | Conditionally YES |
| Typography: Raleway Regular | Inferred from font system | INFERRED | NO — requires approval |
| Title safe zone margin | Not in image | NOT_IN_IMAGE | NO |
| Subtitle safe zone margin | Not in image | NOT_IN_IMAGE | NO |
| Overlay safe zone margin | Not in image | NOT_IN_IMAGE | NO |
| Branding placement | Not in image | NOT_IN_IMAGE | NO |

---

## Asset Matrix

### Universal Graphic Element Rules (enforceable)

| Rule | Value | Confidence | Enforceable |
|---|---|---|---|
| 5 elements exist | compass_rose, ouroboros, classical_columns, drafting_compass, circle_medallion | HIGH | YES |
| Rendering style | Fine line art, no solid fills | HIGH | YES |
| Color | Warm gold (character) on dark background | HIGH | YES (character-based) |
| Specific warm_gold hex | NOT_VERIFIED | NOT_VERIFIED | NO |

### Per-Element Assignment

| Element | Confirmed Use | Confidence | Enforceable | All Other Uses |
|---|---|---|---|---|
| compass_rose | Footer nav separator | HIGH | YES | INFERRED — requires approval |
| classical_columns | Wordmark icon (between lines) | MEDIUM | Partial | INFERRED — requires approval |
| ouroboros | Graphic element (identity role) | HIGH for existence; LOW for specific use | Existence: YES. Specific use: NO | INFERRED — requires approval |
| drafting_compass | Graphic element only | HIGH for existence | Existence: YES | INFERRED — requires approval |
| circle_medallion | Graphic element only | HIGH for existence | Existence: YES | INFERRED — requires approval |

---

## Texture Specification

### Confirmed Rules (enforceable)

| Rule | Confidence | Enforceable |
|---|---|---|
| 6 texture categories exist | HIGH | YES |
| All warm earth tones, no cool or synthetic materials | HIGH | YES |
| Darkest: near-black stone character | HIGH | YES |
| Lightest: warm cream / parchment character | HIGH | YES |
| Remaining 4: archaeological/historical materials | MEDIUM (category identification) | PARTIAL |

### Texture Names (descriptive — not printed in image)

| Position | Assigned Name | Color Range (character) | Category Confidence |
|---|---|---|---|
| Row 1, Col 1 | black_stone_obsidian | Near-black | HIGH |
| Row 1, Col 2 | carved_stone_relief | Dark warm | MEDIUM |
| Row 1, Col 3 | archaeological_surface_detail | Dark-medium warm | MEDIUM |
| Row 2, Col 1 | dark_map_line_texture | Medium warm | MEDIUM |
| Row 2, Col 2 | cracked_earth_aged_plaster | Medium-warm | MEDIUM |
| Row 2, Col 3 | parchment_aged_paper | Warm cream | HIGH |

### Usage Rules

All usage assignments: `NOT_IN_IMAGE` — NOT enforceable until human specification.

---

## Complete Enforcement Table

Every distinct rule in this specification with final enforcement verdict.

| Rule ID | Rule | Tag | Confidence | Enforceable |
|---|---|---|---|---|
| E-T01 | Primary typeface: Cinzel SemiBold | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T02 | Secondary typeface: Raleway Regular | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T03 | Only Cinzel and Raleway permitted | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T04 | Wordmark THE: Cinzel SemiBold | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T05 | Wordmark HYPOGEUM: Cinzel SemiBold | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T06 | Wordmark CHRONICLES: Cinzel SemiBold | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T07 | Tagline: Raleway Regular | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T08 | Footer labels: Raleway Regular ALL CAPS | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T09 | Cinzel renders ALL CAPS only | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-T10 | Cinzel Regular (400) forbidden | INFERRED | — | **NO** |
| E-T11 | Cinzel Bold (700) forbidden | INFERRED | — | **NO** |
| E-T12 | Raleway weights other than Regular forbidden | INFERRED | — | **NO** |
| E-T13 | Shorts overlays: Raleway Regular | INFERRED | — | **NO** |
| E-L01 | Three-line wordmark: THE / HYPOGEUM / — CHRONICLES — | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L02 | HYPOGEUM is largest element | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L03 | THE is smallest line | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L04 | Em-dashes flanking CHRONICLES are mandatory | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L05 | All wordmark lines: Cinzel SemiBold | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L06 | Wordmark color: warm gold on dark (character) | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L07 | Thumbnail placement: horizontally centred | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L08 | Small architectural icon in wordmark | DIRECTLY_OBSERVED | HIGH / MEDIUM | Existence: YES. Form: partial |
| E-L09 | CHRONICLES never scaled larger than HYPOGEUM | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-L10 | End card / video / Shorts placement | NOT_IN_IMAGE | — | **NO** |
| E-L11 | Minimum size | NOT_IN_IMAGE | — | **NO** |
| E-L12 | Clear space / safe area | NOT_IN_IMAGE | — | **NO** |
| E-TH01 | No human faces in thumbnails | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-TH02 | Dark atmospheric background | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-TH03 | Background: stone/ruins/arch/environmental/cartographic | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-TH04 | Wordmark compositionally dominant | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-TH05 | Subtitle below wordmark, subordinate scale | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-TH06 | Four variant types | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-TH07 | No light/bright-key backgrounds | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-TH08 | Max text density (numeric) | NOT_IN_IMAGE | — | **NO** |
| E-TH09 | Numeric position rules | NOT_IN_IMAGE | — | **NO** |
| E-C01 | 7-swatch palette, dark to light | VERIFIED | HIGH | **YES** |
| E-C02 | Warm earth tones only — no cool/neon/saturated | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-C03 | Bright white forbidden | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-C04 | Neon hues forbidden | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-C05 | Cool blues forbidden | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-C06 | Greens forbidden | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-C07 | Pinks and purples forbidden | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-C08 | Light surfaces forbidden | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-C09 | Display text: warm gold character | DIRECTLY_OBSERVED | HIGH | **YES** (character; not hex) |
| E-C10 | Graphic elements: warm gold character | DIRECTLY_OBSERVED | HIGH | **YES** (character; not hex) |
| E-C11 | void_black hex: #080C0E | VERIFIED | HIGH | **Reference only — BGA does not enforce hex** |
| E-C12 | deep_stone hex: #1A1D20 | VERIFIED | HIGH | **Reference only — BGA does not enforce hex** |
| E-C13 | charcoal_earth hex: #2B2E33 | VERIFIED | HIGH | **Reference only — BGA does not enforce hex** |
| E-C14 | ancient_soil hex: #5C4A2F | VERIFIED | HIGH | **Reference only — BGA does not enforce hex** |
| E-C15 | antique_gold hex: #C49A42 | VERIFIED | HIGH | **Reference only — BGA does not enforce hex** |
| E-C16 | warm_gold hex: #E7D6B3 | VERIFIED | HIGH | **Reference only — BGA does not enforce hex** |
| E-C17 | parchment_cream hex: #F5F3EE | VERIFIED | HIGH | **Reference only — BGA does not enforce hex** |
| ~~E-C18~~ | ~~burnished_bronze hex: #7A5C14~~ | ~~NOT_VERIFIED~~ | ~~RETIRED~~ | **Removed — not present in approved Style Board** |
| E-A01 | 5 graphic elements identified | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-A02 | All elements: fine line art, no fills | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-A03 | All elements: warm gold on dark background | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-A04 | compass_rose as footer separator | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-A05 | Usage assignments beyond footer | INFERRED | — | **NO** |
| E-X01 | 6 texture categories exist | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-X02 | All textures: warm earth tones | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-X03 | Darkest: near-black stone | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-X04 | Lightest: warm cream/parchment | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-X05 | Texture usage assignments | NOT_IN_IMAGE | — | **NO** |
| E-P01 | Six content pillars | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-VG01 | On-register imagery types | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-VG02 | Single silhouette in environment: on-register | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-VG03 | Lighting: single directional source, high contrast | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-VG04 | Lighting: warm/natural — no flat studio | DIRECTLY_OBSERVED | HIGH | **YES** |
| E-VG05 | Forbidden visual modes (talking head, lip sync, etc.) | PRE_EXISTING_POLICY | — | Conditionally YES — active in VPD, not yet via BGA |
| E-VG06 | Narration: voice-over only | PRE_EXISTING_POLICY | — | Conditionally YES — active in VPD |
| E-VG07 | Shadow depth 70–90% (quantified) | NOT_IN_IMAGE | — | **NO** — invented value, must be removed |
| E-S01 | Shorts aspect ratio 9:16 | PRE_EXISTING_POLICY | — | Conditionally YES — active in Shorts spec |
| E-S02 | Shorts max_words_per_overlay: 7 | PRE_EXISTING_POLICY | — | Conditionally YES — active in Shorts spec |
| E-S03 | Shorts overlay beats | PRE_EXISTING_POLICY | — | Conditionally YES |
| E-S04 | Shorts safe zone margins | NOT_IN_IMAGE | — | **NO** |
| E-S05 | Shorts typography | INFERRED | — | **NO** |
| E-M01 | Motion language vocabulary (all items) | INFERRED | — | **NO** — entire section; static image |
| E-PB01 | AI disclosure required | PRE_EXISTING_POLICY | — | Conditionally YES — active in channel policy |
| E-PB02 | AI disclosure exact text | NOT_IN_IMAGE | — | **NO** |

---

## Enforcement Summary

| Category | Total Rules | Enforceable NOW | Not Enforceable |
|---|---|---|---|
| Typography | 13 | 10 | 3 (weight rules — INFERRED) |
| Logo / Wordmark | 12 | 9 | 3 (placement/size — NOT_IN_IMAGE) |
| Thumbnail | 9 | 7 | 2 (numeric — NOT_IN_IMAGE) |
| Color character | 10 | 10 | 0 |
| Color hex values | 7 | 7 (reference only — BGA does not enforce hex) | 0 (all VERIFIED) |
| Graphic elements | 5 | 4 | 1 (usage — INFERRED) |
| Textures | 5 | 4 | 1 (usage — NOT_IN_IMAGE) |
| Content pillars | 1 | 1 | 0 |
| Visual grammar | 7 | 4 (character) + 2 (conditional/VPD) | 1 (quantified — NOT_IN_IMAGE) |
| Motion language | All | 0 | All (INFERRED from static image) |
| Shorts | 5 | 0 (conditional only) | 5 |
| Publishing | 2 | 0 (conditional only) | 2 |
| **TOTAL** | **82** | **49 YES + conditional** | **26 NO** |

**Enforceable without any additional input:** 49 rules  
**Conditionally enforceable (active elsewhere in pipeline, not yet via BGA):** 7 rules  
**Not enforceable until verified or specified:** 26 rules  

The 26 non-enforceable rules break down as:
- 8 hex values (NOT_VERIFIED — need eyedrop)
- 8 require human decisions (weight rules, usage assignments, font in Shorts)
- 10 require human specification of values (safe zones, min sizes, AI disclosure text, etc.)
