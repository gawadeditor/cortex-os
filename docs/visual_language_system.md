# Visual Language System — The Hypogeum Chronicles

**Phase:** VISUAL LANGUAGE EXTRACTION  
**Source:** `hypogeum/brand/references/image.png` (primary reference thumbnail)  
**Supporting:** `brand_board.jpg` (color/type ground truth), `thumbnail sample.jpg` (genre context)  
**Extracted:** 2026-06-21  
**Status:** Authoritative — applies to all generation systems

---

## How to use this document

Every section ends with **Prompt Constraints** — copy-pasteable language for injection into
Thumbnail Director, Image Prompt Director, and Video Prompt Director system prompts.
The JSON schema at `visual_language_schema.json` is the machine-readable form of this document.

---

## 1. Subject Hierarchy

### Analysis

The reference thumbnail contains three competing subjects. Their visual weight is not equal and
the inequality is the point — the viewer's eye moves in a specific, designed sequence.

| Subject | Frame area occupied | Visual weight | Eye priority |
|---|---|---|---|
| Hook text ("IT SHOULDN'T EXIST") | ~22% of frame area | **High** — warm gold against near-black | **1st** |
| Hero subject (Antikythera mechanism) | ~38% of frame area | **High** — detailed, occupies right half | **2nd** |
| Environment (chamber, walls, tools) | ~40% of frame area | **Low** — dark, textured, recessive | **3rd** |
| Wordmark ("The Hypogeum Chronicles") | ~2% of frame area | **Anchor** — small, top-center | **4th** |

**Eye path:** Hook text → Hero subject → Environmental depth → Wordmark

The text wins eye priority not from size alone, but from **contrast ratio** — warm gold (#C8960A range)
against near-black shadow (~8 brightness). The hero subject wins second because of **detail density** —
the gear teeth and engraving are information-rich and pull the eye once it leaves the text.

### Calibrated percentages

- Dominant subject (mechanism): **38%** of frame  
- Text zone: **22%** of frame  
- Environmental negative space: **40%** of frame  
- Text optical weight despite smaller area: perceived as **equal to mechanism** due to contrast

### Rules

1. Text must always come first in the eye path — achieved through contrast, not through size alone.
2. The hero subject must occupy no more than 40% and no less than 30% of frame area.
3. 40% of the frame must be dark environmental space — not void, but textured dark space.
4. The wordmark is always last in the eye path — small, anchored top-center or top-right.

### Prompt constraints

```
SUBJECT HIERARCHY:
Hook text reads first (contrast from warm gold against near-black).
Hero subject reads second (right 40-50% of frame, detail-rich).
Environment reads third (dark, textured, 35-40% of frame).
Wordmark is not part of the scene — do not render it as a scene element.
```

---

## 2. Layer Hierarchy

### Analysis

The reference is not a flat scene. It has four physically distinct depth planes, and each plane has
a different character, color temperature, and sharpness level.

| Layer | Elements | Depth position | Sharpness | Temperature |
|---|---|---|---|---|
| **Foreground** | Scattered stone fragments, tool handles, debris at bottom edge | 0–0.5m from camera | Soft / partial | Warm |
| **Midground** | Mechanism (dominant), embedded in stone at the break point | 0.5–2m | Sharp — focal plane | Warm amber + cool verdigris |
| **Background** | Chamber walls, faint astronomical diagrams, stone shelving | 2–8m | Soft, textured | Neutral-warm |
| **Atmosphere** | Torch light bloom (upper-left), volumetric dust particles, vignette | Non-physical overlay | N/A | Orange-amber |

**Critical observation:** The mechanism is partially **occluded** — its left edge disappears into rough
stone/soil. This occlusion creates the midground/background separation and makes the mechanism feel
physically embedded rather than composited onto the scene. Without occlusion, the image looks like
a product shot. With occlusion, it looks like discovery.

### Foreground elements

- Fragmented stone, broken pottery edges at the very bottom edge of frame
- Tool handles or wooden implements partially visible
- Characteristic: **partially cut off** by the frame bottom — implies world extends beyond the image
- Lighting: catches only the ambient warm spill, no direct light

### Midground elements

- Hero subject: the mechanism
- Stone/earth it is embedded in — the embedding material is part of the midground
- The text lives here too (typographically in the midground plane, depth-integrated)

### Background elements

- Rough stone walls with subtle carved detail (astronomical diagrams, texture)
- Ancient wooden shelves or niches barely readable in deep shadow
- No single background element competes with the mechanism for attention
- Background is **dark**, not black — it has texture but low brightness (avg < 40 RGB)

### Atmospheric elements

- Upper-left torch bloom: the brightest single pixel area in the frame (near RGB 255)
- Volumetric dust: warm particles suspended in the torch beam, mid-frame
- Vignette: progressive darkening from center to all four corners
- The vignette is **strong** — corner brightness is < 10% of center brightness

### Rules

1. Every image must have all four layer types — missing any one collapses depth.
2. Foreground elements must be cut off by frame edges — implying space beyond.
3. The hero subject must be partially occluded by its embedding material at one edge.
4. A practical light source (torch, lamp, candle) must be visible or implied.
5. The vignette must be strong enough that all four corners are near-black.

### Prompt constraints

```
LAYER HIERARCHY (required):
Foreground: stone fragments, tool edges, debris — partially cut off by frame bottom.
Midground: hero subject embedded/emerging from rough stone, partially occluded at edges.
Background: chamber walls with faint carved texture, deep shadow, barely readable.
Atmosphere: warm torch bloom from upper-left, volumetric dust particles, strong vignette.
Every corner must be near-black. No bright edges.
```

---

## 3. Depth Model

### Analysis

The reference achieves strong perceived depth through four mechanisms: **focal differentiation**,
**scale graduation**, **occlusion**, and **atmospheric haze**. The focal plane is at the mechanism face.

### Focal plane

- Sharpest elements: the gear teeth, dial engravings, and bronze surface texture of the mechanism
- Second sharpest: the text (typographically integrated — receives the same sharpness priority)
- Everything else: progressively softer

### Blur zones

| Zone | Description | Relative blur |
|---|---|---|
| **In-focus zone** | Mechanism face + front gear teeth | 0 — reference sharpness |
| **Soft foreground** | Debris at bottom edge | Slight softness — not bokeh |
| **Soft background** | Chamber walls | Moderate softness — texture visible but not sharp |
| **Deep background** | Far corners | Heavy softness + darkness — detail lost |

The blur is **subtle**. This is not a portrait photo with shallow depth-of-field. The blur is enough
to separate planes but not enough to destroy environmental texture. Call it: depth-of-field factor 1.5–2,
not 8–10.

### Occlusion zones

- Left edge of mechanism: transitions into rough stone / embedded
- Lower-left: stone and debris cover the mechanism base partially
- The occlusion creates the **discovery** reading — something being unearthed, not displayed

### Silhouette requirements

The mechanism must produce a **strong readable silhouette** at thumbnail scale (320px wide).
The test: if the image is converted to pure black-and-white and scaled to 320px wide,
the mechanism must still be recognizable as "large circular ancient machine."

Rules:
- Circular profile with clearly defined outer ring
- Gear teeth or mechanical detail visible at thumbnail scale
- Strong rim light that defines the silhouette against the background
- The mechanism must NOT blend into the background at its upper/right edges

### Depth rules

1. Focal plane is always the hero subject — not the text, not the environment.
2. Foreground softness is subtle — avoid unrealistic depth-of-field.
3. Occlusion at one edge of the hero subject is required.
4. Hero subject silhouette must be readable in B&W at 320px.
5. Depth cue from atmospheric haze increases toward the background (darker + hazier = further).

### Prompt constraints

```
DEPTH MODEL:
Sharpest element: hero subject (mechanism gear detail, surface texture).
Foreground elements: slight softness, not sharp, partially cut off.
Background: soft, textured, not sharp.
Hero subject partially embedded in/occluded by stone at its left and lower edges.
Strong circular silhouette with rim light on right edge defining it against background.
Readable as a large circular ancient machine at 320px thumbnail width.
Subtle depth-of-field — depth separation without unrealistic bokeh.
```

---

## 4. Lighting Model

### Analysis

The reference uses **cinematic chiaroscuro** — a single warm key light source creates dramatic
directional illumination with deep, rich shadows. The lighting is **motivating**: the torch visible
in the upper-left corner explains every lit surface.

### Key light

- **Source:** Off-camera torch / oil lamp, positioned upper-left of frame
- **Color:** Deep amber-orange, approximately 2200K–2700K
- **Direction:** 45° from upper-left, raking across the mechanism face
- **Intensity:** High — creates bright warm highlights on mechanism surface (gear faces, ring edges)
- **Fall-off:** Rapid — left side bright, right edge falls to near-black

The torch creates a **raking** light direction. Raking means the light travels nearly parallel to
the surface it hits, which reveals surface texture dramatically. This is why the gear teeth and
engraving lines catch light — the raking angle turns micro-surface detail into visible highlight/shadow.

### Rim light

- **Purpose:** Defines the mechanism's right-edge silhouette against the dark background
- **Color:** Slightly cooler than key — neutral warm, not orange
- **Intensity:** Subtle — just enough to separate subject from background
- **Position:** Right edge, approximately 120–150° from key

Without the rim light, the right edge of the mechanism would merge into the background.
The rim light is what makes the silhouette readable at thumbnail scale.

### Practical lights

- **Torch flame:** Visible in upper-left, extremely bright orange bloom
- **Role:** Motivates the key light and adds compositional interest to the upper-left
- **The bloom:** Extends across the upper-left ~10% of frame — warm orange wash
- **Rule:** The practical light source must always be visible or strongly implied

### Shadow behavior

- **Shadow density:** Near-black (< 15 brightness in deepest areas)
- **Shadow color:** Deep warm-neutral, not pure grey or pure black
  - RGB range in deep shadows: approximately (8–25, 5–18, 2–10) — slightly warm even in darkness
- **Shadow edge:** Soft gradients, no hard-edged shadows (diffuse practical source)
- **Fill light:** Minimal — shadows are **intentionally** deep and dark

### Measurement from reference

- Torch area avg RGB: (103, 55, 11) — very warm
- Upper-left corner avg RGB: (70, 37, 7) — still warm but very dark
- Lower-right corner avg RGB: (21, 11, 2) — near-black with faint warm tint
- Wordmark zone avg RGB: (13, 10, 6) — near-pure black
- Overall: **66% of pixels below brightness 25** — this is a fundamentally dark image

### Lighting rules

1. Single key light from upper-left (torch/lamp character, 2200K–2700K).
2. Rim light on the hero subject's right edge — required for silhouette separation.
3. A practical light source (torch, lamp) must be visible in the upper-left zone.
4. Deep shadows — fill light is near-zero.
5. Shadow areas must have a warm (not blue/grey) undertone.
6. 60–70% of frame pixels must be below brightness 50.

### Prompt constraints

```
LIGHTING MODEL:
Single key light: warm amber torch/oil lamp, positioned upper-left, off-camera.
Light direction: 45° from upper-left, raking across subject face to reveal texture.
Rim light: subtle, right edge of hero subject only, separates from background.
Practical light source (torch flame or lamp glow) visible in upper-left of frame.
Shadow behavior: very deep, near-black, warm undertone (not grey or blue).
Fill light: minimal — shadows must be dark, not brightened.
Overall: dark image. 60-70% of the frame is near-black. Light is surgical.
```

---

## 5. Color Model

### Analysis

The color palette is built around a **single temperature axis**: deep warm amber vs rich near-black.
There is no cool color. Even the mechanism's verdigris (green-gold patina on bronze) reads warm in
the amber light. The image feels unified because every color is warm or dark — never cool or bright.

### Primary palette (brand-authoritative, from brand_board.jpg)

| Name | Hex | RGB | Role |
|---|---|---|---|
| **Deep Black** | `#080500` | (8,5,0) | Shadow base, near-black zones |
| **Dark Amber** | `#614520` | (97,69,32) | Warm shadow mid-tone |
| **Gold Dark** | `#908633` | (144,134,51) | Aged gold, secondary highlights |
| **Gold Light** | `#C7B680` | (199,182,128) | Primary gold, text highlights, mechanism highlights |
| **Warm White** | `#BFF3E3` | — | Reserved; near-white only in brightest specular |
| **Teal Accent** | `#8CAAAB` | — | Cold accent — use only in deep shadow on aged metal |

### Secondary palette (environment-specific)

- Bronze/verdigris on mechanism: mix of `#908633` (gold) and a cooled green — reads warm under torch
- Stone texture: desaturated amber-brown, range (40–80, 28–55, 12–24)
- Torch bloom: saturated orange, approximately `#D4620A` to pure amber at center
- Carved detail in stone: near-black with slight warmth, (15–30, 10–20, 5–12)

### Contrast axis

**Warm amber illumination ↔ Near-black warm shadow**

This is the only contrast axis. There is no:
- Blue vs orange
- Light grey vs dark grey
- Saturated vs desaturated (everything has at least slight warmth)

The contrast is purely **luminance** — bright warm vs dark warm. This is what creates the
"ancient firelight" atmosphere without the image feeling garish or oversaturated.

### Temperature split

- **Lit areas:** 2200K–2700K torch warmth — orange-amber
- **Shadow areas:** Slightly warm-neutral, approximately 3500K equivalent
- **No area is cool.** Any accidental blue or grey reads as a mistake.
- Acceptable exception: verdigris patina on ancient bronze — a muted olive-green that reads warm
  in torch light and reads as "aged bronze," not as a cool color

### Color calibration measurements from reference

- Left text zone avg: RGB (72, 52, 25) → R:G:B ratio ≈ 2.9:2.1:1.0 (strong warm dominance)
- Torch area avg: RGB (103, 55, 11) → ratio ≈ 9.4:5.0:1.0 (extreme warmth)
- Shadow zone avg: RGB (21, 11, 2) → ratio ≈ 10.5:5.5:1.0 (warm even in shadow)
- Red/Blue ratio across entire image: consistently > 2.5:1

### Color rules

1. Red/Blue ratio must be > 2.5:1 in all illuminated areas.
2. No pure white — maximum highlight is warm cream, approximately `#E8D4A0`.
3. No cool colors — blue is only acceptable as a vanishing trace in deep shadow.
4. Text color: warm gold range `#B89020` to `#D4A830` — never pure yellow, never white.
5. Shadow color: deep warm-black — RGB (8–30, 5–20, 2–10). Never RGB grey (equal channels).
6. Overall image warmth: desaturated to pure black = wrong; cool shadows = wrong.

### Prompt constraints

```
COLOR MODEL:
Palette: deep warm black (#080500), dark amber (#614520), aged gold (#908633 / #C7B680).
All illuminated areas: warm amber (2200K–2700K torch character).
All shadow areas: deep warm-black — NOT grey, NOT blue. A trace of warm tint in even the darkest shadow.
Text color: aged gold / warm parchment — no pure white, no cool grey.
No cool colors anywhere. No blue. No desaturated grey.
Acceptable exception only: verdigris green-gold on aged bronze surfaces.
Red/Blue channel ratio must exceed 2.5:1 across all lit areas.
```

---

## 6. Typography Relationship

### Analysis

The text in the reference is the **first thing the eye reads** despite being on the left side and
despite the mechanism being physically larger. This is achieved through three mechanisms:
contrast ratio, typographic scale, and material integration.

### Text vs subject balance

| Attribute | Hook text | Hero subject |
|---|---|---|
| Frame area | ~22% | ~38% |
| Perceived weight | Equal or slightly higher | Equal or slightly lower |
| Eye priority | 1st | 2nd |
| Contrast to background | **Highest in image** | High |
| Left/right positioning | Left 35% of frame | Right 55% of frame |

The text wins the eye-priority competition on **contrast**, not on size. The mechanism wins it on
**detail richness** and **size** but loses to text on contrast. This balance is intentional: the
viewer reads the text first (gets the hook), then looks at the mechanism (gets the visual payoff).

### Scale ratios

Two typographic weights coexist in the reference:

**Headline:** "IT SHOULDN'T"
- Smaller, lighter weight
- Occupies approximately the top 30% of the text zone height
- Reads as setup/context

**Emphasis:** "EXIST"
- Dramatically larger — approximately **3× taller** than the headline
- Fills the lower 70% of the text zone
- Reads as the emotional payload

**Measured ratio from reference:** Emphasis height : Headline height ≈ 3.2:1

At 320px thumbnail width, "EXIST" must still be recognizable as large, bold text.
At this scale the emphasis word occupies approximately 30–35% of the thumbnail height.

### Text integration method

The text in the reference is **not a layer pasted over the image**. It has three properties that
integrate it into the scene:

1. **Material texture:** Letters have a cracked stone/distressed surface — the same material
   language as the stone walls and mechanism surface. Letters feel ancient, not digital.

2. **Lighting response:** The letter faces receive the same warm amber torch light as the mechanism.
   The upper face of letters is warm gold; the right-side faces fall into shadow. The text is lit
   by the same key light as everything else.

3. **Shadow casting:** Text casts faint shadow onto the dark background behind it — confirming
   that the text has physical depth, not graphic flatness.

The font style is **serif with heavy weight for emphasis** — consistent with brand (Cinzel Semibold
family from brand_board.jpg). The headline is lighter weight, also serif.

### Spatial relationship

- Text left boundary: approximately 2–4% from left frame edge
- Text right boundary: approximately 35–40% from left frame edge
- The text does NOT overlap the mechanism at any point in the reference
- There is a visible dark gap between the rightmost letter of "EXIST" and the left edge of the mechanism
- This gap is a breathing zone — approximately 5–8% of frame width

### Typography rules

1. Emphasis : Headline height ratio must be ≥ 2.5:1, target 3:1.
2. Text occupies left 35% of frame maximum — never bleeds into the right 60%.
3. Text must have a material surface that matches the scene (stone, aged gold, not graphic white).
4. Text must respond to the scene's key light — same temperature, same direction.
5. 5–8% breathing gap between text right-edge and subject left-edge.
6. At 320px thumbnail width, emphasis word must be readable.
7. No text boxes, no backing panels, no lower-thirds, no subtitle styling.

### Prompt constraints

```
TYPOGRAPHY RELATIONSHIP:
Hook text occupies left 35% of frame, does not overlap hero subject.
Two weights: headline (smaller, lighter) above, emphasis (3× larger, dominant) below.
Text material: aged stone surface with cracked warm gold highlights — same visual language as environment.
Text lighting: receives the same warm amber key light from upper-left as the rest of the scene.
5-8% breathing gap between text right-edge and hero subject left-edge.
No backing boxes, no text panels, no subtitle styling.
```

---

## 7. Thumbnail Laws

These are the non-negotiable rules that define whether an image succeeds as a Hypogeum Chronicles
premium thumbnail. Violation of any law produces an unusable image regardless of individual element quality.

---

### Law 1: The 66% Darkness Rule

**66% or more of the image pixels must be below brightness 25 (near-black).**

The reference achieves 66% near-black. This is not an accident — it is what creates the "ancient
underground chamber" atmosphere. Bright, evenly-lit images look like studio shoots. Dark images
with surgical light look like discovery.

Failure mode: Compositor produces an image that is too bright overall. The environment reads as
a well-lit room rather than a subterranean discovery.

---

### Law 2: Single Warm Key Light — No Fill

**There must be one warm key light and near-zero fill. Shadows must be deep.**

Fill light destroys chiaroscuro. The dramatic quality of the reference comes from the absence of
fill — shadows are allowed to go very dark. The only thing that prevents the image from being all-black
is the warm key light from the torch, applied selectively to the focal subjects.

Failure mode: Multiple light sources, even fill, bright ambient. Image loses drama.

---

### Law 3: Text Reads First at 320px

**At 320px thumbnail width, the hook text must be the first thing a viewer's eye reaches.**

If the mechanism competes with or beats the text for first attention, the thumbnail fails its
primary job (CTR — the hook must land before anything else). The text wins through contrast:
warm gold against near-black.

Failure mode: Text too small, too low-contrast, or obscured by environment detail.

---

### Law 4: The Mechanism Must Be Discoverable, Not Displayed

**The hero subject must be partially embedded in or emerging from its environment.**

A mechanism sitting on a clean surface is a product shot. A mechanism half-embedded in ancient
stone is a discovery. Occlusion at one or more edges is required.

Failure mode: Hero subject floats freely on a clean background, looks like a 3D render.

---

### Law 5: No Empty Voids

**Dark zones must be dark AND textured. Pure black (#000000) areas are forbidden.**

The shadows in the reference contain stone grain, wall texture, and atmospheric haze — they are
dark but not empty. A pure black zone reads as a compositing artifact or a missing asset.

Failure mode: Large flat-black areas in frame, especially in lower-left where text is absent.

---

### Law 6: All Color Is Warm

**No cool colors except vanishing trace amounts in deepest shadow.**

Blue, cool grey, desaturated neutral — all disqualified. Every color in the image is warm or has a
warm tint. This is the single most identifiable characteristic of the visual language.

Failure mode: Shadow areas go cool grey/blue, mechanism reads cold silver instead of warm bronze.

---

### Law 7: Composition Axis Is Left-Text, Right-Subject

**Text is always left. Subject is always right. This axis must never be inverted.**

The composition follows the natural reading direction — eye enters upper-left, reads text, then
moves right to the visual payoff (the mechanism). Inverting this breaks the eye-path design.

Failure mode: Subject on left, text on right. Or centered composition.

---

### Law 8: Zone Compliance

**Top 12% of frame: near-pure black (wordmark overlay zone).**  
**Bottom 10% of frame: near-black, no text, no subject detail.**

The wordmark is overlaid in production. If the image has any content in the top 12%, the wordmark
will be illegible. The bottom 10% must be clear for YouTube progress bar and mobile display reasons.

Failure mode: Content, inscriptions, or bright areas in reserved zones.

---

### Law 9: Silhouette Test

**Hero subject must be identifiable in silhouette at 320px width.**

Convert the thumbnail to pure black-and-white, scale to 320px wide. The hero subject must be
immediately identifiable as its category ("ancient circular machine"). If it is not, the subject
is too small, too embedded, or too similar in tone to the background.

Failure mode: Mechanism blends into background at thumbnail scale.

---

### Law 10: Four Layers Required for Premium Quality

**Every premium thumbnail must have: base_scene + hero_subject + fx + typography layers.**

A single-pass generated image cannot simultaneously optimize for atmospheric environment,
isolated subject clarity, typographic scale, and text material accuracy. Only layered compositing
achieves all four at premium quality.

Failure mode: Single-pass generation. Text is too small, hallucinated, or material-incorrect.
Environment is too generic or too busy (optimized for scene, not for subject isolation).

---

## Composite Gap Analysis: Reference vs. ep003_A_layered

| Attribute | Reference | ep003_A_layered | Gap |
|---|---|---|---|
| Near-black pixel % | 66% | ~42% | **Too bright** — compositor warmth lift needs tuning |
| Left text zone avg brightness | ~58 | ~89 | Composite screen-blend lifting too much |
| Torch practical visible | Yes | Implied (bloom only) | Could be stronger |
| Hero subject occlusion | Left edge embedded in stone | Right edge only, not embedded | Needs occlusion prompt |
| Background detail | Walls with astronomical diagrams | Present but soft | Acceptable |
| Text material | Cracked stone + warm gold | Cracked stone + warm gold | Close match |
| Silhouette at 320px | Strong circular profile | Strong | Match |
| Zone compliance | Wordmark zone near-black | Some atmospheric glow in top zone | Minor violation |

**Primary fix needed:** Reduce overall brightness of composite. The warmth grade + contrast enhance
is brightening the dark areas too much. The reference achieves drama through darkness — our compositor
is producing a composite that is ~30% too bright in shadow areas.

---

## Application Summary

This visual language applies across all generation contexts:

| Context | Primary laws | Key constraint |
|---|---|---|
| **Thumbnail** | All 10 laws | Silhouette at 320px; text reads first |
| **Episode hero image** | Laws 1, 2, 4, 5, 6, 7 | No text — subject + environment only |
| **Video shot (B-roll)** | Laws 1, 2, 4, 5, 6 | No composition split — pure atmosphere |
| **Short-form thumbnail** | Laws 1, 2, 3, 6, 8 | Square format; subject center; text top/bottom |
