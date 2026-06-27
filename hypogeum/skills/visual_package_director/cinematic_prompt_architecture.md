# CINEMATIC PROMPT ARCHITECTURE

When writing `primary_prompt` and `fallback_prompt` for any scene, apply the layered architecture below in the exact assembly order. This architecture supersedes the generic word-count constraint for prompt fields. Prompts written with this architecture are provider-ready long-form specifications, not shorthand briefs.

---

## The Principle

Light is the primary subject of every shot — before objects, before figures. Material detail is the proof of period accuracy. Negative space is a compositional tool, not emptiness. The camera records physical reality; the prompt describes only what the camera records. Never describe what a scene means. Describe only what it shows.

---

## Layer 1 — Premise

One sentence. The cinematic function of the shot — why this frame exists in the episode's visual arc. Written in present tense. Not a description of action; a declaration of what the frame is doing narratively.

Examples:
- "A body breaks the surface of the Aegean for the last time before the world changes."
- "A relic of impossible precision suspended at the edge of recorded history."
- "The moment a two-thousand-year-old machine introduces itself."

---

## Layer 2 — Shot and Subject Declaration

Format: `[Shot type], [subject declaration].`

Shot types (use exact terms): extreme close-up, close-up, medium close-up, medium, medium wide, wide establishing, overhead, macro, low angle

Subject declaration: the primary subject + its state in one clause. Name the physical object, figure, or environment — not its significance.

Examples:
- "Wide establishing, a sponge diver in rough water-darkened canvas gear erupts through the wave surface against the pale limestone Antikythera coastline."
- "Extreme close-up, a single corroded bronze gear fragment with dense green-brown marine patina and copper-orange worn edges occupies the left third of a 16:9 frame against an absolute dark void."

---

## Layer 3 — Lighting Plan

Format: `[Temperature] light [entry verb] from [direction/position], [connector] [illuminated feature]; [falloff].`

**Temperature**: bleached noon white, warm amber, copper-amber golden hour, cool grey-white diffuse, deep warm gaslight, single oil-lamp warmth

**Entry verbs**: enters, descends, arrives, rakes across, falls across, radiates outward

**Connectors** (match source type):
- Overhead/directional: "illuminating the [feature]"
- Raking side: "raking across the [feature]"
- Diffuse/gaslight: "falling across the [feature]"

**Falloff**: hard falloff to pure unlit black / rapid falloff to deep shadow / soft receding to warm dimness / no ambient fill

The falloff sentence must name what recedes into darkness — not just that darkness exists.

Examples:
- "Bleached noon white light descends from overhead, illuminating the wave crests bleaching to white foam and the diver's canvas shirt at the shoulder; hard reflections fracturing across the surface; Aegean water deepens to intense ultramarine below the scatter layer; limestone cliff holds iron-oxide warmth in its deep fissures only."
- "Warm amber light enters from the upper left, raking across the corroded ridges and gear tooth faces, casting micro-shadow relief into the tooth valleys at a low oblique angle; opposing face recedes into deep surrounding shadow with no ambient fill; hard falloff to pure unlit black."

---

## Layer 4 — Material, Texture, and Environment Detail

Format: `Surface, material, and environmental detail: [obs_1]; [obs_2]; [obs_3]. [obs_4].`

Four distinct observations. Each must name a specific material surface and describe its physical state under the scene's lighting. No observation may be generic or repeat the subject declaration.

**Observation 1**: Primary surface — the texture of the main object/material as light hits it. Name the material, its aging or condition, and its light response.

**Observation 2**: Secondary period material — rope, fabric, stone, metal, papyrus, wood — the tactile layer that proves the period. Name grain, salt, wear, weight.

**Observation 3**: Environmental element — water surface, earth, air quality, cliff face — what gives the frame spatial depth beyond the subject.

**Observation 4**: The precision detail — one observation that could not appear in any other period or setting. This is the shot's authenticity proof. Begin after the period from obs_3.

Examples from s01:
- "rough canvas shirt — salt-bleached along the shoulders, charcoal-heavy where seawater saturates the fabric fully; layers of salt service visible in the fibre texture;"
- "hemp rope at the diver's waist — still beaded with water, fibres darkened by salt immersion, individual strands visible in the noon light;"
- "pale limestone cliff face at frame-right — iron-oxide streaks running vertically, dried sea-salt crust above the waterline, shadow gathering only in deep fissures."
- "Aegean water surface — intense ultramarine in the deeper scatter layer, wave crests bleached to near-white foam at the diver's emergence point."

---

## Layer 5 — Spatial Composition and Negative Space

Three statements in sequence:

1. **Background**: What fills and recedes in the background. Name the dominant background element and how it fails or pulls away from the frame.
2. **Subject position**: Where the primary subject sits in the frame. Use rule-of-thirds language: upper-left quarter, lower-right third, centre-to-left, one-third of frame width.
3. **Negative space**: What the negative space is and where it breathes. Negative space is not emptiness — it is a compositional counterweight. Name it.

Format: `Spatial composition: [background]. Composition: [subject position]. Negative space: [where and what].`

---

## Layer 6 — Camera and Motion Plan

Format: `Camera: [verb]. Subject: [subject behavior]. [Motion qualifier].`

**Camera verbs**: static, slow pan, push in, pull back, slow tilt, overhead descent, handheld drift

**Subject behavior**: what the primary subject physically does during the clip — not what it means, what it does. A fragment does not "convey age." It sits, catches light, holds position, recedes to shadow.

**Motion qualifier**: the secondary motion that gives the frame life without camera movement — light temperature shift, water surface settling, dust particle drift, fabric drape weight, thermal heat shimmer. This is what makes a "static" shot feel alive.

---

## Layer 7 — Time Segmentation (8-second clips only)

Two phases, each exactly 4 seconds. The transition must be a physical change — something visible resolves, shifts, or deepens. Not an emotional label.

Format: `0-4s: [what happens in first half]. 4-8s: [what changes or completes in second half].`

The first half establishes. The second half deepens, shifts, or releases.

Examples:
- "0-4s: diver's torso clears the water surface, canvas gear shedding heavy sheets of seawater, wave disturbance radiating outward from the point of emergence. 4-8s: the disturbed water surface settles into open ultramarine, the diver receding to frame-left as the wake calms and the Aegean horizon opens fully at the right."
- "0-4s: fragment emerges from absolute darkness as light resolves detail across the corroded face, patina micro-texture sharpening progressively from the brightest lit gear tooth inward toward the shadow. 4-8s: light temperature deepens from amber to copper-orange, shadow edges harden along gear teeth, void consolidates to pure negative space behind the fragment."

---

## Layer 8 — Technical Tags

Append inline at the end of the prompt in brackets:

`[4K] [24fps]` — always present

`[macro]` — only for extreme close-up / macro shots

`[depth of field: <value>]` — state the focal range: "shallow — gear teeth to patina face on one focal plane", "wide — surface to horizon", "medium — figure to bench surface"

`[color grade: <pair>]` — state the dominant warm and subordinate cool as a pair: "bleached noon white against ultramarine blue", "warm amber-copper against pure black", "warm atrium daylight, white wool and ochre-cream tones"

`[aspect ratio: 16:9]` or `[aspect ratio: 9:16]` — match the scene's aspect_ratio field

---

## Layer 9 — Negative Constraints

4–6 constraints that name the specific anachronisms or compositional errors most likely to appear for this scene type. Written as `[without X]` or `[no X]`.

Target what AI video models default to for this setting — museum lighting for artefacts, fluorescent for interiors, modern coastline infrastructure for seascape shots, synthetic materials for period figures.

Examples:
- `[without modern watercraft] [without contemporary diving equipment] [no wetsuit] [no modern coastline infrastructure] [no synthetic rope]`
- `[without modern surfaces] [without institutional display context] [no ambient fill] [no background detail] [no lens flare] [no motion blur] [no museum case or display hardware] [no label or scale reference]`

---

## Assembly Order — primary_prompt (Seedance / MUAPI)

Assemble all layers into a single continuous paragraph with no line breaks:

```
[Premise] [Shot + subject.] [Lighting entry + falloff.] [Material detail: obs1; obs2; obs3. obs4.] [Spatial composition: bg. Composition: position. Negative space: breath.] [Camera: verb. Subject: behavior. Qualifier.] [0-4s: phase1. 4-8s: phase2.] [4K] [24fps] [...tags] [...negative constraints]
```

---

## Assembly Order — fallback_prompt

The fallback must render reliably when the primary fails at a lower tier. Reduce complexity:

- Retain: Layer 1 premise (shortened), Layer 2 shot + subject, Layer 3 lighting (entry only, drop falloff detail), Layer 9 negative constraints
- Drop: material detail layers 3–4, time segmentation, technical tags
- Target: 80–120 words. Same period accuracy as primary. Same negative constraint list.

The fallback is not a lazy version of the primary. It is a simpler scene that achieves the same narrative function through a less demanding visual.

---

## Validation Checks Before Committing a Prompt

- [ ] Light is described before the subject is described
- [ ] At least one material observation names a specific texture under specific lighting
- [ ] Negative constraints name the AI model's most likely default failure for this setting
- [ ] The 0–4s / 4–8s transition is a physical change, not an emotion
- [ ] No word in the prompt is "mysterious", "important", "significant", "ancient" without a physical qualifier
- [ ] The fallback would render if the primary fails — it is a distinct achievable scene, not a truncated version
