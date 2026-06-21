# Thumbnail Style System — The Hypogeum Chronicles

Reverse-engineered from:
- `references/brand_board.jpg` — channel identity and color system
- `references/thumbnail sample.jpg` — genre-performing reference thumbnail
- ep_003 candidates — current output gap analysis

---

## 1. Composition

### 1.1 Subject Size

**Reference (thumbnail sample):** The Poseidon statue occupies approximately 50–60% of the right frame area. It fills the vertical height of the frame from roughly 10% from top to 90% from top. It is NOT small.

**Brand board visual style:** Silhouette figure occupies ~8% of frame height — the scene is vast architecture and sky; the human proves the scale of what surrounds it. The stone corridor image fills the frame with environmental depth.

**Rule:** There are exactly two valid approaches.

- **Approach A — Monumental Subject:** Subject fills 45–65% of right frame. Scale is heroic. Object or figure is large enough to read as a recognizable shape at 320px. Backgrounds are in context, not void.
- **Approach B — Scale Contrast:** Subject is tiny (5–12% of frame height), placed at rule-of-thirds intersection. Surrounding architecture or environment is the protagonist. The subject proves the scale of the world around it.

**Forbidden:** An isolated medium-sized object sitting in pure black void. This reads as a product shot, not a mystery.

### 1.2 Subject Placement

- Primary subject: right 40–60% of frame (rule of thirds, right column)
- Human or scale anchor: center-right, just above midline or at midline
- Text zone: left 35–45% of frame, anchored at left edge with padding ~6–8% of frame width
- Top 10–12% reserved for wordmark — no subject detail should enter this zone

### 1.3 Negative Space Usage

Negative space is not the subject. It is a structural tool. Use it only to:
1. Create the text landing zone (left 35–45%)
2. Create depth recession behind the subject
3. Preserve wordmark legibility at top

The remainder of the frame must contain visual content with texture, depth, atmosphere, or implied environment. A frame that is 70–80% pure black void with a small object reads as minimalist gallery art, not YouTube mystery content.

### 1.4 Camera Distance and Angle

- **Distance:** Medium to medium-close. Subject should fill its zone. Not micro-detail macro; not wide establishing where the subject is lost.
- **Angle:** Slightly low (5–15° below eye level looking up) or direct eye level. The low angle creates heroic, monumental, threatening. Direct eye level creates confrontational, uncanny, equal.
- **Forbidden camera positions:** Top-down (product shot angle), extreme close-up showing only texture detail, wide shot where the object is a speck with nothing else in frame.

### 1.5 Framing Rules

- Frame must be FULL. No compositional padding for its own sake.
- Subject must be anchored to the right column, bleeding toward center.
- If architecture is in frame, it must establish environmental depth (perspective lines, receding walls, doorways, sky).
- Bottom 8–10% is text safe zone when hook text is placed low. Do not let subject or background detail compete with text in this zone.

---

## 2. Lighting

### 2.1 Number of Light Sources

**Reference:** Three distinct sources — storm-sky (cool diffuse), hero spot on statue face (warm directional), underwater glow (teal).
**Brand board:** One dominant shaft from above-center, strong fall-off, deep shadow everywhere else.

**Rule:** Two sources minimum. One is always the primary key light (warm, directional). The second provides separation, rim, or environmental atmosphere. A third may add cool or cold contrast from the opposite temperature.

### 2.2 Direction

- **Primary key:** 30–60° from left or right, elevated 20–40° above horizon. This produces half-lit faces/objects with dramatic shadow — the signature of theatrical mystery.
- **Secondary rim or fill:** From opposite side, cooler temperature, much lower intensity (20–35% of key). This prevents subjects from reading as flat against background.
- **Never:** Frontal flat fill. Overhead studio ring. Soft box style even diffuse. Anything that eliminates shadow.

### 2.3 Rim Light Behavior

Rim light defines the silhouette of the subject against the dark background. It is mandatory. Without it, the subject merges into the void and loses readable form at thumbnail scale.

- Intensity: bright enough to clearly define the edge (not just a hint)
- Color: 400–500K cooler than the key (if key is amber 2700K, rim is 3200K–3500K). Alternatively, rim can be white-silver if the key is very warm.
- Coverage: defines top shoulder / side edge / partial top of head for figures; defines top and one lateral edge for objects

### 2.4 Contrast Level

- Black point: true near-black (#080808–#141414) in shadow regions, not lifted grey
- Highlight point: 85–100% luminance on the single brightest specular hit; not full clip (which looks cheap)
- Contrast ratio: approximately 8:1 to 12:1 between key-lit zone and shadow zone
- Luminance histogram: most mass in the 5–20% range; thin peak at 75–90% for highlights; nothing substantial in 20–60% (midtone) range

### 2.5 Highlight Behavior

Highlights should behave like firelight or torch — small point sources with rapid fall-off, warm golden-amber color in the hottest specular region, not white. The second-brightest element in the frame after the subject highlight should be the hook text.

---

## 3. Color

### 3.1 Dominant Palette (from brand board)

| Role | Hex | Description |
|------|-----|-------------|
| Void | #080808–#141414 | True near-black, not raised |
| Deep stone | #1F1A14 | Warm-tinted dark mid |
| Shadow mid | #2E2519 | Dark brown-black transitions |
| Bronze accent | #836333 | Only on lit surfaces of objects |
| Gold accent | #C4A642 | Highlight peaks, warm-lit metal |
| Text primary | #F5E3B9 | Parchment cream, NOT pure white |
| Text emphasis | #E8C96A–#D4B55A | Warm gold for emphasis word |

### 3.2 Accent Palette

One optional accent that is NOT amber is permitted to create visual tension:
- **Storm/cool accent:** #4A6080 to #6A8CB0 — used in sky, atmospheric haze, or secondary environment only. Never on the primary subject.
- **This accent should occupy no more than 15–20% of frame area**

### 3.3 Saturation Limits

- Subject: 20–40% saturation in shadow zones; 40–60% in lit zones. Not de-saturated to grey; not hyper-saturated to appear CGI.
- Background/environment: 15–30% saturation. Must feel real-world, not rendered.
- Text: saturation is not a lever here — value (lightness) does the work. Text color is warm cream or warm gold.
- Forbidden: Electric blue glow, neon accent, hyper-chromatic VFX gradient, fully de-saturated monochrome.

### 3.4 Warm/Cool Balance

Thumbnail must have dominant warm temperature (overall read: amber/gold/dark-brown). One cool secondary element is allowed for contrast — typically the sky or atmospheric background. The subject and all text must remain in the warm register.

---

## 4. Typography

### 4.1 Text Size

This is where the current system is most wrong.

**Reference:** "REAL" and "ATLANTIS?" each have character heights of approximately 12–15% of frame height. At 720px tall, that is 86–108px per character. This is extremely large.

**Target for this brand:** Emphasis word character height should be 9–12% of frame height (65–86px at 720p). Headline text should be 4.5–6% of frame height (32–43px).

**Current system failure:** The compositor renders emphasis at a base font size of roughly 4.5% of frame height, scaled by 2.5× = 11.25%. That should be correct — but the font itself (Copperplate/Cinzel) has much tighter letterforms than Impact, so the perceived visual weight at the same numeric size is dramatically lower. The emphasis word needs to use a heavier font, larger point size, or wider letter-spacing to match the visual impact.

### 4.2 Line Breaks and Word Hierarchy

Two-line structure:
- **Line 1 (headline):** The setup. 3–4 words max. Rendered at 4.5–5.5% frame height. Weight: Regular or SemiBold.
- **Line 2 (emphasis):** The payload. 1–2 words. Rendered at 9–12% frame height. Weight: Bold or ExtraBold. This word must be immediately readable at 320px thumbnail size — it is the first word the viewer reads.

### 4.3 Weight Hierarchy

- Emphasis word: Bold minimum, ExtraBold preferred. Must have visual weight that stops the eye.
- Headline word: SemiBold or Regular. Should recede slightly behind emphasis.
- Wordmark: Small, refined — its weight should be lower than headline text.

Currently the compositor uses Copperplate (all-caps serif) for both overlay text. This font does not have Bold or ExtraBold variants on macOS. The compositor needs to switch to a font with a genuine weight range: Helvetica Neue Bold/ExtraBold, or load Cinzel Bold if available.

### 4.4 Text Placement

- Left-aligned text block anchored to left edge, 6–8% padding from left
- Text block occupies left 35–45% of frame horizontally
- Vertically: bottom 30–35% of frame
- Emphasis word baseline should sit approximately 8–12% from bottom of frame
- Headline sits directly above emphasis, gap of 4–8px

### 4.5 Text Visibility

Text must remain legible on a background that is not pure black. Use a localized dark backing (not a full-width band). The backing should match the natural shadow zone where the text is placed — it should look like shadow, not a UI element.

### 4.6 Safe Margins

- Top: 12% reserved for wordmark. No text or busy subject detail.
- Left: 6–8% padding before text starts
- Bottom: 6–8% below emphasis baseline
- Right: subject zone; no text enters right 55% of frame

---

## 5. Psychological Pattern

### 5.1 Reference Thumbnail (REAL ATLANTIS?) — Decoded

**Curiosity trigger:** The word "REAL" creates instant cognitive challenge. The viewer already knows Atlantis is fictional; "REAL" forces a contradiction they must resolve.

**Emotional trigger:** Scale awe (massive ancient god figure) + discovery anxiety (this might actually be real) + storm drama (existential threat implied by the environment).

**Information gap mechanism:** The question mark is literal but the gap runs deeper. "If this is real, then everything I thought I knew is wrong." The viewer clicks not to answer "is Atlantis real" but to resolve the identity threat.

**Why someone clicks:** The image creates a 0.3-second loop in the viewer's mind: REAL? → but Atlantis isn't real → but this looks real → I need to know. The statue's physical scale makes the claim credible. The viewer must watch to restore cognitive order.

### 5.2 Target Psychological Pattern for Hypogeum

For each concept trigger:

**Impossibility:** The thumbnail must show something that LOOKS REAL but whose existence violates what the viewer knows. The object must appear credible and ancient, in context. The text "IT SHOULDN'T EXIST" works only if the visual confirms something exists that shouldn't. A product-shot mechanism against black says "here is an object" — not "this violates history."

**Loss:** The thumbnail must create a felt absence. Something that was once enormous and important is now gone. This requires: a trace of what was, not just void. The trace makes the loss legible.

**Betrayal:** Something is present that was supposed to not be, or something that was present has been removed. The frame should feel like a crime scene — where something was, or something that shouldn't be.

### 5.3 CTR Psychology Rules

1. The emphasis word must be readable without clicking. It does the first job (stop the scroll).
2. The image must be specific enough to be credible but ambiguous enough to be unresolved.
3. The viewer must sense they are missing information. The thumbnail provides evidence; the video resolves it.
4. The image must feel REAL (photographed, not rendered), ancient, and physically present — not illustrated or digital-art.

---

## 6. Object Treatment

### 6.1 Detail Visibility

**Reference:** The Poseidon statue shows beard detail, trident structure, clothing folds. It is highly readable as a specific thing. Detail level: high.

**Target:** Object must be identifiable as a specific category of thing (mechanism, artifact, monument, inscription). Enough detail to be credible. Too much detail turns it into a showcase; the key details must be specific to its impossible nature.

**Rule:** The one or two features that prove the impossibility must be visible. Everything else can fall into shadow.

### 6.2 What to Hide

Everything that makes the object look like it belongs in its time period. Shadow the base, the mount, the supporting context. Let the impossible-precision part catch the key light. The shadow conceals the parts that are safe; the light reveals the parts that shouldn't be there.

### 6.3 Silhouette Importance

At 320px wide, the object must read by silhouette. The silhouette must communicate the CATEGORY of object, not the fine detail. The Antikythera Mechanism silhouette should read as "complex mechanism" not "rectangular box" and not "corroded lump."

**Current failure (candidate 1):** The mechanism is rendered as a rectangular metal box with gears visible. The silhouette reads as "safe" or "strongbox" not "2,000-year-old precision computer." The object fails to communicate its identity through silhouette.

### 6.4 Texture Visibility

Texture serves one purpose: it makes the object feel real and ancient. Bronze corrosion, stone erosion, aged metal — these textures communicate age and materiality. Keep them in mid-tone; they should not compete with the structural features of the object.

### 6.5 Scale Relative to Frame

- If using Approach A (Monumental): object height = 70–90% of frame height
- If using Approach B (Scale Contrast): object height = 8–15% of frame height
- Forbidden: object at 30–50% of frame height — this reads as product-shot scale, neither monumental nor dwarfed

---

## 7. Forbidden Characteristics — Explicit

Based on analysis of ep_003 candidates against reference:

### 7.1 Candidate 1 ("IT SHOULDN'T EXIST") — Failures

1. **Object reads as product shot:** Mechanism on invisible surface, photographed against black backdrop at eye level. Identical to commercial product photography. No environmental context.
2. **Silhouette reads as "safe" or "strongbox":** The rectangular form with visible gears does not communicate "2,000-year-old precision computer" at thumbnail scale.
3. **Too much amber rim glow:** The warm amber edge light turns the mechanism golden. At thumbnail scale this reads as "ornate golden box" not "impossible ancient mechanism."
4. **Object at 30–35% of frame area:** Neither heroic nor scale-contrast. It occupies the most ambiguous size range.
5. **No scale anchor:** Nothing in the frame establishes that this object is unusual in size, age, or context.
6. **Text weight too light:** "IT SHOULDN'T" in Copperplate/light serif at current size is likely illegible at 320px width. The emphasis word "EXIST" needs significantly more visual weight.

### 7.2 Candidate 2 ("THE KNOWLEDGE VANISHED") — Failures

1. **No legible subject:** At thumbnail scale (320px), a faint amber dust trail on a near-black background may render as a dark rectangle. There is no focal point for the eye to land on.
2. **Looks like abstract wallpaper or VFX element:** The dust/particle trail reads as a motion-design element or screensaver, not as a conceptual argument.
3. **Watermark artifact visible at bottom:** A text element from the image generation model is visible at the bottom of the frame.
4. **Background too warm overall:** The frame reads as dark amber-brown rather than near-black, weakening contrast for both the text and the dust element.
5. **No credibility signal:** This thumbnail could be about anything. There is nothing specific to ancient knowledge, astronomy, or mechanism. The concept of "loss" is not visually grounded.

### 7.3 Candidate 3 ("NO ONE KNOWS WHO BUILT IT") — Failures

1. **Circular spotlight/halo is a fatal flaw:** The large warm amber circle behind the object reads as a commercial product spotlight. This is the most recognizable signature of low-budget product photography. Immediately wrong.
2. **Object does not read as ancient mechanism:** The subject silhouette reads as a camera, lighter, or modern device. At 320px it will be ambiguous. The Antikythera Mechanism should be recognizable as complex, gear-based, bronze.
3. **Image is too warm — no contrast axis:** When the halo fills the center-right with warm amber and the background is dark amber-brown, there is insufficient contrast to separate subject from environment.
4. **Looks like concert poster or album art:** The circular spotlight combined with centered-right object placement is a visual cliché of music industry poster design, not ancient mystery.
5. **No spatial depth:** The circular gradient has no recession, no perspective. The frame reads as flat.

### 7.4 System-Level Failures (across all three)

1. **No human presence or scale anchor.** None of the three candidates include a human figure or human-scale architecture. The brand board references show this is critical — the silhouette image in the brand board visual style panel demonstrates scale contrast using a tiny human in vast landscape.
2. **Wrong subject type.** The Antikythera Mechanism as a physical object is not visually interesting at thumbnail scale. It is a corroded bronze fragment. What is visually interesting is the CONTEXT that makes it impossible — ancient Greek environment, correct-era objects, and then this thing which doesn't belong. The object divorced from context loses its impossibility.
3. **Images look CGI / concept art.** None of the three images could be mistaken for a photograph. They look like digital renders, which reduces credibility. The reference thumbnail looks photographic even though it is likely composite.
4. **Text hierarchy inadequate at target viewing size.** The emphasis word at current rendered size is too small. Text must be the dominant visual element by pixel area in the left 40% of frame.
5. **No cool accent.** All three are monochromatic warm amber. The genre-performing reference uses warm + cool contrast to create tension and visual interest. Monochromatic warmth reads as atmospheric wallpaper.

---

## 8. Candidate Scoring Against Style System

| Category | C1 IT SHOULDN'T EXIST | C2 KNOWLEDGE VANISHED | C3 NO ONE KNOWS WHO |
|---|---|---|---|
| Subject size (correct approach) | 4 — mid-size, wrong zone | 0 — no subject | 3 — mid-size, product scale |
| Subject placement | 7 — right zone correct | N/A — no subject | 6 — center-right, ok |
| Negative space usage | 6 — intentional but empty | 2 — entire frame void | 4 — circular glow fills space |
| Scale anchor present | 0 — no scale reference | 0 — no subject at all | 0 — no scale reference |
| Environmental depth | 1 — pure void, no environment | 0 — pure void | 2 — gradient only |
| Primary lighting direction | 6 — lateral, correct | 3 — diffuse ambient | 3 — overhead spotlight |
| Rim light present | 7 — yes, amber rim | 0 — none | 2 — halo is not rim |
| Number of light sources | 4 — single warm only | 2 — ambient only | 3 — single spotlight |
| Cool/warm contrast | 0 — monochrome amber | 1 — mostly warm | 0 — monochrome amber |
| Black point integrity | 7 — near-black background | 5 — warm dark but not black | 3 — brown-amber, not black |
| Object silhouette at 320px | 3 — reads as box not mechanism | 0 — no readable silhouette | 2 — reads as camera/lighter |
| Credibility (looks real) | 5 — plausible but CGI | 2 — abstract VFX | 3 — obviously CGI |
| Text size / emphasis weight | 3 — too small, too light | 5 — correct zone, ok size | 4 — ok size, low contrast |
| Text legibility at 320px | 4 — headline likely lost | 6 — emphasis readable | 5 — reads but thin |
| Information gap mechanism | 6 — "shouldn't exist" implies one | 3 — no visual anchor | 4 — hook good, image doesn't land |
| Genre-correct energy | 2 — quiet, atmospheric | 1 — abstract, no energy | 2 — commercial-feeling |
| **Total** | **59/160** | **30/160** | **46/160** |

---

## 9. Gap Analysis

### Critical gaps (0–3 scores):

1. **No scale anchor** — all three score 0. Nothing in the frame communicates the scale of what the object represents.
2. **Environmental depth** — all three score 0–2. Pure void or flat gradient; no recession, no world.
3. **Cool/warm contrast** — all three score 0–1. Monochromatic amber across the board.
4. **Object silhouette at 320px** — all score 0–3. None of the objects read correctly at target size.
5. **Genre-correct energy** — all score 1–2. None would stop a viewer who is browsing the mystery/history genre.
6. **Credibility (looks real)** — all score 2–5. All read as digital renders or concept art.

### What the current pipeline is optimizing that it shouldn't be:

The concept-driven_mode rules emphasize: darkness, void, minimalism, single object, atmospheric emptiness. This is the wrong target. These qualities produce gallery art or concept art — not YouTube CTR.

The gap between the current output and the reference thumbnail is not a gap in prompt words. It is a gap in understanding of what the visual grammar of this genre requires: **scale, credibility, energy, and environmental specificity**.

---

## 10. Required Pipeline Changes

### 10.1 Agents/Prompts That Must Change

#### thumbnail_director.txt — SECTION 11C (CONCEPT_DRIVEN_MODE)

Remove or replace:
- "darkness, void, negative space as active compositional elements" — this is producing empty frames
- "At most ONE clearly visible object or figure per prompt" — this produces isolated product shots
- "Near-black (void_black) occupies 75–90% of the frame" — this is too dark; 55–70% is more appropriate
- The explicit instruction to avoid showing the historical object — the object must be shown, but correctly

Add:
- Subject must be in an environmental context (not isolated against void)
- Object must be visible enough to establish credibility at 320px width
- At least one human figure or recognizable architectural scale anchor must be present
- Image should look photographed, not rendered or illustrated
- Second light source must be present: a cool accent (storm light, diffuse grey, moonlight) to create temperature contrast with the warm key
- Subject must occupy either 45–65% of right frame (Approach A) OR 5–12% of frame height as tiny figure in vast environment (Approach B)

#### thumbnail_director.txt — SECTION 11B (LIGHTING)

Add: "A second, cooler secondary light source is required. This may be diffuse skylight, storm atmosphere, reflected stone grey, or moonlight. The warm primary and cool secondary must create visible temperature contrast."

#### thumbnail_composition_planner.py

The planner currently maps every trigger to `focal_scale: "small"` (impossibility) or `focal_scale: "absent"` (loss). These values feed into prompts that produce the current failures.

Change:
- `impossibility` → focal_scale: `"large"` or `"monumental"`, focal_zone: `"center_right"`, environment: `"ancient_context"`
- `loss` → add "trace_element" requirement — void cannot be the only content; some remnant must be present
- `betrayal` → focal_scale: `"medium_large"`, environment required

#### thumbnail_typography_planner.py and compositor

- Emphasis word: increase base size from 4.5% to 7–8% of frame height before scaling
- Headline word: switch font to one with genuine Bold/ExtraBold variants (Helvetica Neue Bold, or use a heavier variant of the current font)
- Consider adding white stroke/outline to emphasis word to ensure legibility against any background

### 10.2 What Must NOT Change

- Left-zone text placement — this is correct
- Warm/dark overall palette — this is correct for brand identity
- Stacked headline/emphasis structure — this is correct
- Wordmark position and scale — this is correct
- Near-black background as base — this is correct; just reduce void from 80% to 55–70%

### 10.3 The Single Most Important Change

Replace the instruction "Darkness is the medium; near-black occupies 75–90% of the frame" with:

**"The environment is the medium. The subject exists in a real ancient world. Near-black occupies 50–65% of the frame; the remaining 35–50% shows environmental context — architecture, atmosphere, stone, sky, or water — that makes the subject's existence feel impossible by contrast."**

This single change addresses the root cause of all three candidates' failure: they are images of objects in nothing, not images of impossible things in a world that proves their impossibility.
