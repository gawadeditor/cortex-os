# Video Style System — The Hypogeum Chronicles

**Document version:** 1.2  
**Created:** 2026-07-07  
**Hierarchy position:** Visual Design Principles → Brand Board → **Video Style System** → Brand Guardians → Validators → Runtime  
**Sibling document:** `thumbnail_style_system.md`  
**Parent document:** `visual_design_principles.md`

---

## 1. Purpose

This document is the sole canonical source for every rule that governs video clip generation inside The Hypogeum Chronicles production pipeline.

Every rule in every downstream runtime module — scene planner, presentation planner, shot planner, prompt writer, brand guardian, style validator, render QA — must be traceable to a section of this document. If a runtime rule exists without a traceable section here, it is architectural drift.

This document does not contain prompt syntax, provider payloads, API parameters, or implementation code. It contains only canonical behavioral specifications.

---

## 2. Scope

**In scope:**
- Visual identity of generated video clips: color, lighting, contrast, atmosphere
- Camera language: movement, angle, framing, lens behavior
- Composition rules: subject scale, placement, environmental context, depth
- Motion language: what camera movements are permitted and why
- Realism rules: documentary register, CGI prohibition
- Historical integrity: period authenticity, anachronism prohibition
- Audio policy: what generated clips may and may not contain
- Clip planning rules: duration, coverage, hold tolerance
- Validation philosophy: what must be checked before and after generation
- Failure library: documented failure modes with visual signatures

**Out of scope:**
- Thumbnail visual rules (see `thumbnail_style_system.md`)
- Typography, wordmark, overlay rules (see `thumbnail_style_system.md`)
- Narration, music composition, audio mixing (separate audio system)
- Platform metadata, publishing rules
- Provider-specific API parameters (see runtime provider modules)
- Prompt syntax (see `cinematic_prompt_architecture.md`)

---

## 3. Relationship to Visual Design Principles

This document implements `visual_design_principles.md` for the video medium. Each section cites the originating principle(s) using the reference P1–P20.

| Principle | Section(s) in this document |
|---|---|
| P1 Documentary Visual Philosophy | §5, §18 |
| P2 Historical Credibility | §17, §18, §28.3 |
| P3 Emotional Architecture | §4, §7, §8 |
| P4 Curiosity Generation | §12, §13, §17 |
| P5 Perceived Realism | §18, §28.1, §28.2 |
| P6 Scale Perception | §14, §15 |
| P7 Lighting Psychology | §8, §9 |
| P8 Color Psychology | §7 |
| P9 Spatial Storytelling | §13, §15, §20 |
| P10 Visual Hierarchy | §13, §14 |
| P11 Human Attention Guidance | §11, §13 |
| P12 Cinematic Restraint | §12, §19, §33 |
| P13 Material Authenticity | §16 |
| P14 Environmental Storytelling | §15 |
| P15 Visual Continuity | §4, §7, §34 |
| P16 Information Density | §13, §20 |
| P17 Negative Space Philosophy | §13, §19 |
| P18 Motion Philosophy | §19 |
| P19 Atmosphere Philosophy | §10 |
| P20 Viewer Trust | §18, §27, §29 |

---

## 4. Brand Identity

**Principles:** P3 Emotional Architecture, P15 Visual Continuity

**Why.** The channel's visual identity is the accumulated impression across every piece of content. A viewer who encounters one episode and returns for the next must recognize the same visual world. Identity is not a single frame or color — it is a consistent register maintained across all frames.

**Identity statement.** The Hypogeum Chronicles is a cinematic archaeological documentary channel. Its visual world is defined by: ancient physical materials, warm directional light, deep shadow, atmospheric evidence of inhabited space, and the consistent presence of darkness that implies more than is shown. The register is that of a camera present at an archaeological site — not a reconstruction studio, not a museum exhibit, not a game environment.

**Brand tone.** Dramatic restraint. Every frame should feel like it cost effort to obtain and contains more than it immediately reveals.

**What the brand identity is not:**
- It is not a history-infotainment channel (bright, educational, clean)
- It is not a supernatural mystery channel (neon, blue glow, sci-fi aesthetics)
- It is not a cinematic recreation channel (broad dramatic recreations, actors, dialogue)
- It is not an archaeological lecture series (diagrams, maps, talking-head explanations)

---

## 5. Documentary Identity

**Principle:** P1 Documentary Visual Philosophy

**Why.** The channel positions its content within the documentary tradition. This tradition has a specific visual contract with the viewer: what is shown was recorded, observed, or witnessed — not constructed for the camera. Generated video content breaks this contract by definition. The documentary register is the set of visual choices that restore the appearance of that contract: footage that looks observed rather than designed, environments that look inhabited rather than built, evidence that looks found rather than staged.

**The documentary test.** Every frame must pass: does this look like it could have been photographed? Not whether it was photographed, but whether a viewer would believe it could have been.

**Documentary register requirements:**
- Surfaces must show material age: wear, oxidation, mineral staining, asymmetry
- Light sources must appear to be motivated by the physical environment of the scene, not by a production crew
- Camera behavior must appear to follow a subject or explore a space, not perform for the viewer
- Atmospheric conditions must reflect the physical reality of the space: underground heat haze, exterior dust, moisture on stone

**What breaks the documentary register:**
- Surfaces that look digitally generated or computer-modeled
- Lighting that has no physical source within the scene
- Camera movements that have no narrative motivation
- Spaces that are too clean, too symmetrical, or too perfectly composed
- Any visual element that could not physically exist in the claimed period and setting

---

## 6. Historical Identity

**Principle:** P2 Historical Credibility

**Why.** The viewer brings period knowledge to every frame. Visual anachronisms — wrong materials, wrong light behavior, wrong spatial construction — register subconsciously even when the viewer cannot name them. A single anachronism breaks the frame's credibility.

**The credibility hierarchy.** Accuracy is not the same as credibility. Perfect historical accuracy in a digitally rendered space is less credible than a slightly imprecise approximation in a physically real-looking space. Credibility is the higher priority.

**Period accuracy requirements:**
- Construction materials must be period-appropriate: Roman Imperial (travertine, tuff, Roman concrete, fired brick, timber, hemp, iron, bronze)
- Light sources must be period-appropriate: torch, oil lamp, brazier, natural daylight, moonlight — no electric light, no industrial light, no modern reflective surfaces
- Spatial geometry must reflect period construction knowledge: Roman arches, brick vault tolerances, hydraulic engineering, not modern structural precision
- Human elements (figures, tools, clothing) must be period-accurate if visible; otherwise, exclude them

**What constitutes a visual anachronism:**
- Modern construction precision on surfaces that should show handwork variation
- Even or ambient light with no period-plausible source
- Materials that belong to a different period (steel, reinforced concrete, glass, neon)
- Spatial scale that exceeds known period engineering capabilities without the visual evidence that makes that scale plausible
- Cleanliness inconsistent with the period function of the space

---

## 7. Color System

**Principle:** P8 Color Psychology, P15 Visual Continuity

**Why.** The channel's color palette encodes meaning. Warm tones (amber, gold, ochre) are the colors of fire, ancient material, and inhabited space — they signal that the viewer is inside the ancient world. Cool tones (stone grey, atmospheric sky, diffuse overcast) signal the outside, the modern, the observed distance. The palette is not decorative; it is semantic.

### 7.1 Channel Identity Palette

These values are derived from the brand board and are fixed across all episodes. They represent the channel's visual identity, not a single period or setting.

| Zone | Hex Range | Description |
|---|---|---|
| Deep void / shadow | #080808–#141414 | True near-black. Not lifted grey. Dominant background tone. |
| Warm dark mid | #1F1A14–#2E2519 | Warm-tinted dark — the boundary between void and illuminated stone |
| Bronze / aged metal | #836333 | Only on lit surfaces of period metalwork |
| Amber / gold key light | #C4A642–#D4AF37 | Highlight peaks on warm-lit stone and metal |
| Cool atmospheric accent | #4A6080–#6A8CB0 | Sky, stone-grey reflected light, overcast atmosphere |

### 7.2 Warm/Cool Balance

**Rule:** Warm tones dominate. Cool tones are secondary accents, not competing headlines.

- Warm-illuminated zones (amber, gold, ochre, terracotta): 35–50% of frame area
- Dark/near-black shadow zones: 50–65% of frame area
- Cool atmospheric accent: maximum 15–20% of frame area; never on the primary subject

**Why the 50–65% dark ceiling.** Frames where 70–80%+ of the frame is near-black void read as gallery art or abstract visual design — not documentary evidence. The subject must exist in an environment, and that environment must occupy 35–50% of the frame. Darkness beyond 65% eliminates environmental context and makes the subject into an isolated object.

**Why the 15–20% cool accent ceiling.** More than 20% cool creates a cool-dominant or balanced frame. The channel's warm dominance must be unmistakable — the viewer must always read "ancient fire" before they read "atmospheric observation."

### 7.3 Period-Specific Palette Variation

Individual episodes may define period-specific palettes (e.g., Imperial Rome vs. 1930s archaeological excavation) that operate within the channel identity palette as subsets. These period palettes may shift the warmth of the key light or the character of the cool accent but may not:

- Replace the near-black shadow zone with any lighter dominant
- Make cool tones the primary lighting source
- Introduce any color outside the warm earth — cool stone range (no green, no purple, no electric blue, no synthetic palette)

### 7.4 Forbidden Color Behaviors

- Electric blue, neon blue, or any synthetic cool palette as primary tone
- Green-dominant palette or green lighting
- Pink, purple, or magenta dominant
- Fully de-saturated monochrome (reads as stylized, not documentary)
- Hyper-saturated colors (reads as CGI or motion graphic)
- Uniform warm orange with no variation across the tonal range (reads as Instagram filter)

---

## 8. Lighting System

**Principle:** P7 Lighting Psychology

**Why.** Light is the primary narrative tool in the channel's visual language. Darkness is not the absence of subject matter — it is the presence of the unknown. Warm directional light defines what is known and visible. The space between light and darkness is where the channel's meaning lives. Every lighting setup must manage this boundary deliberately.

### 8.1 Source Count and Function

**Minimum:** Two light sources in every scene. One warm key (primary). One cooler secondary (atmospheric separation, rim definition, or environmental context).

**Why two sources.** A single light source produces no separation between subject and background — the subject merges into the dark field. The secondary source defines the silhouette of the subject against the dark, making it readable at any playback scale.

**Maximum:** Three sources in exceptional scenes (key + rim + atmospheric fill). More than three sources eliminates the directional drama that defines the channel's lighting character.

### 8.2 Primary Key Light

**Character:** Warm, directional, motivated by a period-plausible physical source.

**Temperature:** Amber/torch equivalent — approximately 2,700K–3,200K. The warmest point in any frame is always the primary subject under the key light.

**Direction:** 30–60° lateral (left or right of camera axis) and elevated 20–40° above the subject horizon. This angle produces the half-lit surfaces with dramatic shadow that define the channel's visual character. Frontal flat lighting is always forbidden.

**Motivated sources:** torch, oil lamp, brazier, candlelight, underground fire, single shaft of desert or Mediterranean sunlight penetrating an opening, golden-hour exterior raking light.

**Falloff requirement:** The key light must fall off sharply. The illuminated zone (key-lit subject area) should not exceed 35–45% of the total frame. The falloff from maximum illumination to near-black should be abrupt — the character of firelight and focused directional sources, not the gradual falloff of diffuse ambient.

### 8.3 Secondary Light Source

**Character:** Cooler than the key (not cold; cooler). Provides silhouette separation, edge definition, or atmospheric environmental context.

**Temperature:** 200–500K cooler than the primary key. If the key is torch amber (2,700K), the secondary is warm-grey to stone-grey (3,200K–4,500K). Never pure blue-white or cool blue.

**Permitted secondary sources:** diffuse overcast sky entering through an opening, stone-grey reflected light from adjacent surfaces, diffuse underground cold air, moonlight (cool silver — not blue), heavy overcast Mediterranean sky, storm atmosphere.

**Forbidden secondary sources:** electric blue, neon, scanning or X-ray glow, bioluminescent blue, spectral blue-white, any synthetic or technological light source.

**Position:** From the opposite direction to the primary key or from above/ambient. Never positioned to compete with the key for dominance on the primary subject.

### 8.4 Forbidden Lighting Behaviors

- Flat frontal fill: eliminates shadow and drama
- Overhead studio ring: produces even illumination with no directional character
- Soft box diffuse: institutional and commercial register
- Uniform ambient: no single source, no direction, no falloff — the opposite of the channel's character
- Fluorescent or institutional even lighting: modern register, non-documentary
- Any lighting setup where the entire frame is illuminated to the same level

### 8.5 Underwater Scenes

When a scene takes place in or near water, the primary light source is still warm: torch/dive-lamp amber dominates. Teal/blue is a subordinate ambient only — it may appear in peripheral atmospheric zones but must never be the headline descriptor of the lighting. The warm/cool balance rule applies underwater as above ground.

---

## 9. Contrast System

**Principle:** P7 Lighting Psychology

**Why.** Contrast is what makes the subject legible, the darkness compelling, and the frame credible as photography. A low-contrast frame reads as a processed, grade-heavy digital aesthetic. High directional contrast reads as physically real light in a physically real space.

### 9.1 Shadow Behavior

**Black point:** True near-black in shadow regions — #080808–#141414. Shadows must not be lifted to grey. A lifted shadow reveals the artificial nature of the light (it has no physical source that would produce this even fill); it is the signature of studio ambient fill, not of torch or firelight.

**Shadow edges:** Semi-hard to hard, depending on source distance. Torchlight and firelight produce semi-hard shadow edges with slight penumbra. The sun through a narrow opening produces very hard shadows. Diffuse overcast produces soft edges in exterior ambient zones, but the primary key shadow retains its directional character.

### 9.2 Highlight Behavior

**Character:** Firelight and torchlight highlights are small point sources with rapid falloff. The single brightest specular hit on a surface should reach 85–100% luminance but should not clip to pure white — full-clip white reads as a photographic error or a digital artifact.

**Warm highlight color:** The hottest specular hit should be warm amber or warm gold, not neutral white. White highlights signal studio lighting or digital generation.

**Highlight extent:** The key-lit zone should not exceed 35–45% of the total frame area. If more than 45% of the frame is in key-lit warm tones, the darkness has been eliminated and the mystery register is lost.

### 9.3 Contrast Ratio

**Target:** Approximately 8:1 to 12:1 between the key-lit zone and the deepest shadow zone. This produces the tonal range that reads as physically plausible torch/firelight in a stone interior.

**Luminance histogram profile:** Most mass in the 5–20% luminance range (shadow). A thin bright peak at 75–90% luminance (highlights). Minimal mass in the 20–60% midtone range. This profile is the fingerprint of the channel's lighting character — it is what documentary evidence of ancient interior spaces looks like.

---

## 10. Atmosphere

**Principle:** P19 Atmosphere Philosophy

**Why.** Atmosphere — airborne particulate, haze, moisture condensation, combustion smoke — is forensic evidence of inhabitation. A space without atmospheric density looks newly constructed. Ancient spaces that were used by fire, human labor, and water management leave atmospheric evidence. This evidence is what makes a generated space feel real.

### 10.1 Atmospheric Requirements

**Underground and enclosed spaces** must include at least one of:
- Suspended particulate (stone dust, ash, fine debris) visible in the key light beam
- Haze from combustion (torch smoke, brazier emissions) in upper mid-frame
- Moisture evidence on surfaces (condensation, damp stone face, water marks)

**Exterior spaces** must include at least one of:
- Airborne dust (sandy environments, excavation sites)
- Atmospheric haze at depth (heat shimmer over stone, horizon softening in Mediterranean climate)
- Environmental particulate (pollen, fire smoke, sea spray at distance)

**The test:** If the atmospheric condition were removed from the frame, would the space look equally plausible? If yes, the atmospheric condition is decorative. If no, it is functional — it is the evidence of the space having been lived in. The channel requires functional atmosphere.

### 10.2 Atmospheric Behavior

Atmosphere interacts with light sources in specific ways that confirm the physical reality of both:
- Torch smoke drifts upward and accumulates in vaults — it does not hang in uniform mid-air
- Dust in a key light beam traces the direction and angle of the light source
- Moisture on stone surfaces produces reflective highlights that vary across the surface — not uniform sheen

These behavioral specifics are what separate documentary atmosphere from decorative atmosphere. The atmosphere must behave according to physical laws consistent with the scene's conditions.

### 10.3 Forbidden Atmospheric Behaviors

- Uniform mist or fog that fills the frame equally — reads as a filter effect, not a physical condition
- Smoke or haze that behaves independently of the light sources — reveals it is composited, not physically present
- Completely clean air in a space that physically would contain particulate (underground combustion, excavation sites)
- Atmospheric effects that create a cool-dominant visual mood (blue fog, cold mist) — these belong to a different genre register

---

## 11. Camera Language

**Principle:** P11 Human Attention Guidance, P18 Motion Philosophy

**Why.** In the channel's documentary register, the camera's purpose is to follow evidence, explore spaces, and reveal structure. These functions determine what the camera does: it advances toward subjects it is discovering, retreats from subjects it is abandoning, and moves laterally along subjects it is surveying. Every movement has a narrative function that determines its physical form.

### 11.1 Camera Height

**Standard height:** Slightly below eye level — approximately 5–15° downward looking angle on the primary subject. This produces a slight upward appearance on large subjects (monumental, heroic presence) and a slight downward recession on small subjects within large environments (scale contrast). It is the height at which a person standing in the space would observe it.

**Low angle:** Applied for subjects requiring maximum scale or architectural dominance. Camera positioned 15–45° below the subject midline. Appropriate for: vault ceilings revealing height from floor level, architectural facades establishing civic scale, mechanisms or objects viewed from a position that emphasizes their physical scale.

**High angle:** Applied for subjects requiring spatial comprehension — the viewer needs to understand spatial relationships that cannot be read from ground level. Appropriate for: tunnel junction networks, plan views of multi-chamber systems, overhead documentation of archaeological layers. Must not be used as a default establishing approach.

**Overhead:** Applied specifically for surface documentation (sediment layers, plan view of a site, mechanical section from above). Never for general establishing shots — overhead eliminates the spatial experience of being in the space.

**Forbidden heights:** Extreme worm's-eye (looking straight up) except where the ceiling vault is the primary subject. Top-down overhead for general establishing shots.

### 11.2 Camera Movement — Narrative Functions

Every camera movement must serve a specific narrative function. The function determines the physical form of the movement.

| Narrative Function | Correct Camera Behavior | Physical Form |
|---|---|---|
| Discovering a subject | Camera advances toward it | Slow push-in |
| Abandoning or retreating from a subject | Camera withdraws | Controlled pull-back |
| Surveying a surface or structure | Camera moves laterally along the surface | Lateral dolly or slow pan |
| Descending into or through a structure | Camera descends | Vertical descent |
| Ascending from or toward | Camera rises | Vertical ascent |
| Revealing architectural scale | Camera moves back from a close position | Pull-back or wider establishing |
| Revealing hidden structure through cross-section | Camera tilts through the section plane | Slow tilt along the cut axis |
| Following a continuous structure through depth | Camera advances along the axis | Slow forward push |
| Encircling a subject to reveal its three-dimensionality | Camera arcs around the subject | Orbital arc |
| Establishing location or spatial context | Camera holds a wide static position | Controlled hold (maximum 8 seconds) |

**One movement type per scene.** Each scene is assigned exactly one camera movement type, determined by the scene's single narrative function. When a scene appears to serve multiple narrative functions simultaneously, the planner must commit to one. Movement types do not combine — two concurrent movements cannot serve one narrative function without splitting the viewer's attention and obscuring both claims.

### 11.3 Forbidden Camera Behaviors

- **Static hold as the primary camera movement for non-establishing scenes.** A static camera reading as "the camera didn't move because nothing was happening" is not documentary — it is a frozen graphic.
- **Unmotivated drift.** Slow drift without a directional destination — the camera moves but does not arrive anywhere.
- **Decorative orbital rotation.** Circling a subject for visual interest rather than to reveal its three-dimensional form.
- **Simulated handheld for period drama.** Handheld drift is appropriate only for POV-type exploration of a space — where the narrative function is the subjective experience of moving through an unfamiliar space — not as a stylistic substitute for motivated movement in standard scenes.
- **Split or composite camera paths.** Two cameras, two camera behaviors composited — this produces the split-screen visual failure mode. One camera, one continuous movement.
- **Dutch tilt / canted horizon.** A canted camera angle imposed for visual tension rather than to follow the physical geometry of the subject. The channel's documentary register excludes stylistic angle distortions — the camera observes physical reality from a natural orientation consistent with how a person standing in the space would observe it.

### 11.4 Maximum Hold Duration

**Rule:** No camera hold may serve as the visual for more than 16 seconds of narration. A hold beyond 16 seconds produces a visually frozen frame — the physical stillness becomes noticeable and the documentary register breaks down. This is a planning rule, not just a post-production concern. Scenes whose narration beat exceeds 16 seconds require multiple clips or an explicit looping annotation in planning.

---

## 12. Lens Language

**Principle:** P12 Cinematic Restraint, P4 Curiosity Generation

**Why.** Lens choice determines how much information the frame reveals. A wide lens in close quarters reveals the full space; a long lens in a wide space isolates a subject from context. Each choice creates or destroys the curiosity gap. The channel's lens choices are determined by whether more information or less information serves the frame's purpose in the episode's arc.

### 12.1 Shot Scale — Permitted Types

| Shot Scale | Narrative Function |
|---|---|
| Extreme close-up | Material evidence — surface texture under light; the single detail that proves impossibility. The viewer cannot yet determine the context. |
| Close-up | Specific subject identification — readable object or figure at full legibility. Used when the subject itself is the evidence. |
| Medium close-up | Subject in partial context — the subject is primary but the environment begins to appear. |
| Medium | Subject and immediate environment — equal weight. Used for mechanisms in operation, figures in activity, objects in context. |
| Medium wide | Subject within larger environment — the environment establishes the subject's scale and situation. |
| Wide establishing | Environment as primary subject — the space, the landscape, the architectural mass. The subject may be small within it. |
| Overhead | Plan view — spatial relationships that cannot be read from within the space. |
| Macro | Extreme surface detail — material evidence at magnification. Individual fibers, mineral structure, corrosion layers. |
| Low angle | Scale revelation — the subject is seen from below, establishing vertical dominance. |

### 12.2 Depth of Field

**Philosophy:** The channel does not use shallow depth of field as a stylistic signature. Shallow focus is used only when depth compression is the narrative choice — when a foreground subject must be isolated from a background that would compete with it for the viewer's attention. This is a functional use, not a visual style.

**Default:** Wide to medium depth of field. The environment behind the subject is visible and contributes spatial information. The viewer sees the space, not just the subject.

**Forbidden:** Extreme shallow focus as a default beautification technique — this reads as commercial photography or cinematic recreation, not documentary evidence.

---

## 13. Composition

**Principle:** P10 Visual Hierarchy, P11 Human Attention Guidance, P16 Information Density, P17 Negative Space Philosophy

**Why.** Composition is the management of visual hierarchy: what the viewer looks at first, what they look at second, and what they sense without looking at directly. In documentary content, composition must guide the viewer through a deliberate sequence — primary subject → environmental context → implied unknown — without appearing designed. The composition must feel observed.

### 13.1 Primary Subject Placement

**Default:** Primary subject positioned in the right 40–60% of the frame, at or near the horizontal midline. This placement creates natural visual tension: the left portion of the frame holds the implied unknown or the context that gives the subject meaning.

**Centered subject:** Permitted for scenes of architectural symmetry, confrontational subjects, or formal documentation of a site from axis. Centered compositions read as statements of fact — not of mystery.

**Left-dominant placement:** Permitted for subjects that lead the viewer's eye deeper into the frame (a figure walking away, a tunnel extending to the right).

### 13.2 Frame Filling

**Rule:** Frames must be filled with visual content. Near-black and void occupy 50–65% of the frame — this is productive darkness (deliberate shadow zones, atmospheric depth recession) not empty space. The remaining 35–50% must contain environmental content: architectural surfaces, atmospheric depth, subject detail, or period material.

**Forbidden:** Frames where 70–80%+ of the total area is uniform near-black with a single isolated subject. This produces the gallery-art failure mode: beautiful but not documentary, not informative, not credible as evidence.

### 13.3 Layers of Depth

**Rule:** Every frame must contain at least two spatial layers: a foreground element (near) and a background element (far). Three layers are preferred: foreground, midground subject, background depth recession.

**Why.** Two-dimensional flat composition reads as a graphic or illustration. Three spatial layers produce the depth cues that make a frame read as a photographed space. The depth layers are the primary evidence of physical reality.

**Foreground elements:** Architectural elements (a column base, a corridor wall edge, a vault rib entering the frame), atmospheric haze catching the near-field light, a period material surface occupying the near-field edge.

### 13.4 Visual Hierarchy

**Rule:** In every frame, there is exactly one primary subject. All other elements are: environmental context (supports the subject's meaning), atmospheric depth (establishes the space), or negative space (implies the unknown beyond the visible).

**Competing elements:** Two elements of equal visual weight in the same frame produce ambiguity. The viewer cannot determine which is the subject and which is the context. This ambiguity is not mystery — it is confusion. Mystery requires a legible subject in an illegible context.

---

## 14. Subject Scale

**Principle:** P6 Scale Perception, P10 Visual Hierarchy

**Why.** The physical scale of ancient subjects — the height of hypogeum vaults, the span of a Colosseum tier, the mass of a mechanism — is integral to the meaning of the channel's content. Scale is not a visual preference; it is an argument. Showing the vault without conveying its height is not showing the vault — it is showing a tunnel. Scale perception is the difference between evidence and illustration.

### 14.1 Two Valid Scale Approaches

**Approach A — Monumental:** The subject is large within the frame, occupying 45–65% of the vertical height. This approach is used when the subject itself requires close reading — when its material detail, its physical condition, or its internal structure is the primary evidence. The environment remains visible and contextual, but the subject dominates.

**Approach B — Scale Contrast:** The subject is small within a vast environment, occupying 5–15% of frame height. The environment is the primary evidence — the scale of what surrounds the subject is what the viewer must understand. A human figure at 8% of frame height in a vault 20 meters tall is the proof that the vault is 20 meters tall.

**Forbidden:** Subjects at 20–40% of frame height with no environmental context. This is the product-shot zone — the subject is too large to read as evidence-in-context and too small to read as a close examination. It reads as documentary staging, not documentary observation.

### 14.2 Scale Anchors

**Rule:** Every main episode scene must contain at least one scale anchor — a visual element that allows the viewer to calibrate the spatial scale of the environment: a human figure or silhouette; a doorway, arch, or opening of recognizable proportion; a period implement (torch bracket, rope pulley, timber beam) of recognizable scale.

**Why.** Without a scale anchor, the viewer cannot determine whether they are looking at a model, a room, or a vast infrastructure. The Colosseum hypogeum is incomprehensible without the scale evidence that makes its two levels and hundreds of chambers physically imaginable. The scale anchor converts a visually interesting space into a spatially legible one.

---

## 15. Environmental Storytelling

**Principle:** P9 Spatial Storytelling, P14 Environmental Storytelling

**Why.** The channel's subjects do not exist in isolation — they exist in the physical context of the civilization that built them. An artifact without context is an object. An artifact in its original environment is a document. The environment is the evidence that the subject is historically real.

### 15.1 Environmental Context Requirement

**Rule:** Every subject must exist within a plausible ancient physical environment. Pure void backgrounds are not permitted for main episode content. The environment need not be fully visible — atmospheric haze, partial architectural elements, surface textures at the frame edge — but it must be present and period-plausible.

**Exception:** Extreme close-up macro shots of material surfaces (sediment layers, corrosion surfaces, textile weave) may fill the frame entirely with the subject material. In these cases the surface itself is the environment.

### 15.2 Environment as Evidence

**Rule:** The environment surrounding the subject must provide spatial information that the subject alone cannot. The environment answers questions: where is this? What was done here? Who was here? How was this space used?

**Spatial evidence requirements:**
- Architectural geometry: the shape of the vault, the direction of the corridor, the relationship between levels
- Construction evidence: materials, jointing methods, repair patches, structural load paths
- Use evidence: worn surfaces, soot on ceilings, water channels, organic debris
- Scale evidence: elements that calibrate the spatial dimensions (see §14.2)

### 15.3 Forbidden Environmental Conditions

- **Generic ancient space:** A vault or corridor with no identifiable construction period, material system, or spatial relationship to other elements. This produces a set, not a site.
- **Pristine environment:** A space that shows no evidence of use, time, or physical process. Ancient spaces are not clean. They show the accumulation of what happened in them.
- **Anachronistic elements:** Modern construction materials, contemporary lighting fixtures, modern drainage, glass, steel, uniform concrete (as opposed to Roman opus incertum).

---

## 16. Material Authenticity

**Principle:** P13 Material Authenticity

**Why.** Materials are the primary forensic evidence in the channel's visual language. The specific visual signature of travertine under raking torch light — the warm amber catch on raised surfaces, the cool shadow in the recessed joints — is what makes a Roman scene credible. Generic "stone" with no specific material behavior is illustration, not evidence.

### 16.1 Material Specificity Requirements

**Rule:** Every scene must contain at least one material observation that is specific to the period, environment, and physical conditions of the scene. The material observation must describe how the material behaves under the specific lighting conditions of the scene — not just what the material is.

**Period materials for Imperial Rome contexts:**
- Travertine: warm cream-to-ochre surface, dense and smooth on cut face, porous and cratered on rough face; raking light reveals sharp shadows in surface topography
- Tuff (volcanic tufa): dark grey-green to grey-brown; soft and rough-cut; moisture-darkened at base of underground excavations
- Roman concrete (opus incertum): irregular aggregate of tuff fragments in white lime mortar; the mortar joints are thicker and more irregular than modern pointing
- Fired brick: terracotta-red; slightly variable in color across a single course; laid in thin lime mortar lines
- Timber: ancient timber shows grain compression, axe-cut rather than saw-cut marks, checking along the grain, warm amber-brown darkened by age and moisture
- Hemp rope: warm beige-brown, individual fibers visible at close range, fraying at terminations

**Archaeological excavation contexts (19th–20th century):**
- Compacted soil: dark brown to grey-brown; dry fissured surface; visible stratigraphy layers in cross-section
- Crumbling mortar: pale grey-white powdering; fragmenting at exposed faces; different from fresh mortar
- Debris rubble: mixed limestone fragments, terracotta tile shards, bone, seed material — visible at close range

### 16.2 Material Condition Requirements

**Rule:** Materials must show their age and use history. Surfaces that look new, freshly cut, or factory-finished break historical credibility.

**Age evidence indicators:**
- Surface discoloration from mineral deposition (calcium carbonate staining, iron ochre runs, soot blackening)
- Physical wear from foot traffic, rope abrasion, water flow, tool use
- Biological evidence: lichen on exterior surfaces, algal growth at waterline, root penetration at mortar joints
- Structural response to time: subsidence cracks, vault creep, arch keystone displacement

---

## 17. Historical Authenticity

**Principle:** P2 Historical Credibility, P4 Curiosity Generation

**Why.** The channel's curiosity gap depends on the viewer believing that the subject is real and historically significant. Anachronism breaks this belief. The specificity of historical detail is what transforms a dramatic visual into evidence.

### 17.1 Anachronism Prohibition

**Absolute prohibitions (no exceptions):**
- Modern light sources: electric, incandescent, fluorescent, LED
- Modern construction materials: steel reinforcement, modern concrete, plate glass, modern plastic
- Modern surface finishing: machine-smooth plaster, industrial-grade uniformity, contemporary paint
- Contemporary human elements: modern clothing, contemporary tools, post-period artifacts visible in frame
- Modern infrastructure visible in frame: visible signage, railings, ventilation ducts, safety equipment

### 17.2 Disputed Historical Interpretations

**Rule:** Where historical evidence is contested or uncertain, the visual must represent the most conservative defensible interpretation — not the most dramatic. The channel's credibility depends on not over-claiming.

**Substitution protocol:** When the most accurate visual representation is not achievable (a mechanism whose exact form is unknown; a space whose configuration has been destroyed), substitute the closest period-plausible approximation and flag it as such in the production record (visual_integrity_log). The substitution must be visually defensible — it must not assert as fact what is uncertain.

### 17.3 Face Prohibition

**Rule:** No frontal face shots of any human figure — ancient or modern, real or reconstructed, living or sculpted. This prohibition covers: living individuals, ancient figures, royal or imperial portraits, mummified remains, sculptural portraits, fresco faces, mosaics with legible faces as primary subject.

**Why.** Faces create parasocial engagement and personalizing identification — these responses belong to drama, not documentary. The channel's subjects are systems, structures, and hidden truths — not individuals. Human figures appear as silhouettes, working figures from back or profile, hands engaged in period tasks, or crowd elements at distance.

**Substitution:** Replace face-forward portrait subjects with: objects of office or rank, architectural or spatial context associated with the individual, written records (papyrus, inscription, carved relief — not shown in close legible detail), representative artifacts.

---

## 18. Realism Rules

**Principle:** P5 Perceived Realism, P1 Documentary Visual Philosophy, P20 Viewer Trust

**Why.** The most common single failure mode in AI-generated historical content is the CGI appearance: surfaces that look rendered rather than photographed, lighting that looks designed rather than motivated, environments that look built rather than discovered. The channel's documentary register cannot survive this failure. Perceived realism is not a secondary concern — it is the condition under which all other visual rules function.

### 18.1 CGI Appearance — Full Prohibition

**Rule:** Generated clips must not look like CGI, computer-rendered, or digitally animated content. Any visual signature of computer generation is a failure.

**CGI signatures to prohibit:**
- Surfaces that are uniformly smooth or geometrically perfect
- Lighting that illuminates all surfaces from the same general direction without falloff variation
- Materials that have a uniform shader appearance rather than individual surface variation
- Environments that are too symmetrical, too clean, or compositionally too perfect
- Highlights that are too specular, too perfectly shaped, or too uniform across similar surfaces
- Ambient occlusion that is too perfect — dark corners that are exactly as dark as the rendering engine produces them, rather than modulated by light bounce and atmospheric scatter

### 18.2 Illustration and Concept-Art Prohibition

**Rule:** Generated clips must not read as illustrations, concept art, digital paintings, or animated artwork.

**Illustration signatures to prohibit:**
- Stylized rendering (painterly textures, brushstroke-like surface treatment, posterization)
- Color grading that reads as intentional artistic processing rather than photographic observation
- Excessive atmospheric glow or bloom effects that read as visual design rather than physical light behavior
- Subject outlines that are too clean or too defined — real photography has soft, imprecise edges at depth

### 18.3 Game-Engine Prohibition

**Rule:** Generated clips must not look like they were rendered in a real-time game engine.

**Game-engine signatures to prohibit:**
- Hyper-clean surface normals with perfectly tiled textures
- Lighting rigs with too-perfect three-point setups
- Ambient occlusion baked into surface crevices in a uniform, obviously computed way
- Reflections and specular highlights that are too precise or too consistent
- Environmental scale relationships that feel optimized for player navigation rather than physical reality

### 18.4 The Photographic Test

**Rule:** Before a clip is accepted as meeting the realism standard, it must pass: "Could a filmmaker have photographed this?" The answer must be yes — the lighting is physically plausible, the materials are physically present, the atmospheric conditions are physically real, the camera movement is physically motivated. If any element could not exist in physical space, the clip fails the test.

---

## 19. Motion Language

**Principle:** P18 Motion Philosophy, P12 Cinematic Restraint, P17 Negative Space Philosophy

**Why.** In the channel's visual language, motion is not decoration — it is the medium through which the camera's relationship to the subject is expressed. A camera that moves toward something is making a claim: this matters, we are approaching it, it is about to reveal something. A camera that retreats is making a different claim. Unmotivated motion makes no claim and contributes nothing.

### 19.1 The Static Camera Problem

**Rule:** A static camera is permitted only when the subject itself contains all the motion required by the scene. Even in these cases, a slow controlled push-in or minimal drift is preferred over a fully locked camera, because a totally static camera reads as a rendered still image — not as footage.

**The distinction:** A camera that holds position but has a living frame (atmospheric particulate moving through the light, haze shifting, a rope under tension vibrating) is not a static camera — it is a held frame with ambient motion. This is permitted. A camera with no movement and no ambient motion in the frame is a frozen frame — this is not permitted as primary visual for more than 8 seconds.

### 19.2 Temporal Structure of Clips

**Rule:** Every 8-second clip must contain a physical change between its first half (0–4s) and second half (4–8s). The first half establishes a spatial, material, or atmospheric condition. The second half develops, shifts, or completes that condition through physical change.

**Physical changes that satisfy this rule:**
- Camera position change (the camera has moved to a new position by the 4s mark)
- Light condition change (a torch flicker, a shadow edge shift, a light shaft moving)
- Subject state change (a rope becoming taut, dust settling, water level rising)
- Revealed information (the frame contains different visible content at 4s than at 0s)

**Physical changes that do not satisfy this rule:**
- Emotional narrative change ("the mood shifts from wonder to dread") — this is not a physical change
- Editorial change (the narration moves on to a new point) — this is not a physical change within the clip

### 19.3 Atmospheric Motion as Secondary Motion

**Rule:** Every clip must contain at least one secondary motion element in addition to (or in lieu of) camera movement: torch smoke drifting, dust particles moving through a light beam, water surface disturbed, haze shifting, fabric or rope responding to air movement, sand settling.

**Why.** Secondary motion confirms that the space is physically real and that time is passing within the frame. A frame with no motion except camera movement reads as a model or a set. A frame with ambient secondary motion reads as a real physical environment observed in real time.

### 19.4 Camera Energy Across the Episode Arc

**Camera energy** is the combined pace, motion range, and movement intensity of the camera across the episode's clips — whether the camera moves slowly and contemplatively, deliberately and purposefully, or with urgency.

**Rule:** Camera energy may remain constant or increase as an episode progresses toward its narrative climax. Within a continuous section of an episode, camera energy must not decrease abruptly without a deliberate editorial reason (section break, location shift, a scene of deliberate stillness after revelation).

**What constitutes camera energy:** the type of movement selected (slow push-in versus orbital arc versus held establishing), the speed of that movement (controlled and slow versus measured versus urgent), and the spatial range covered within the 8-second clip. These variables in combination determine whether a clip reads as contemplative, purposeful, or urgent.

**Permitted direction:** energy may hold constant or intensify within an episode arc. It may reset to lower energy at the start of a new episode section. An episode that opens with wide, slow establishing movements and closes with more purposeful push-ins and close-material reveals has a natural energy arc. An episode whose energy collapses from forceful movements to hesitant holds in its final scenes has experienced an unintentional visual regression.

**Energy decrease is not always a failure.** A scene of deliberate stillness following a revelation — where a static hold is used to let the evidence settle — is a valid editorial choice. The prohibition is against inadvertent energy collapse caused by planning failures, not against deliberate pacing choices.

---

## 20. Depth Language

**Principle:** P9 Spatial Storytelling, P16 Information Density

**Why.** Video exists in time, not just space. Depth is communicated through camera movement and the revelation of spatial layers as the camera moves — not only through static compositional placement. The channel uses depth language to reveal the physical complexity of ancient spaces: the second level below the first, the corridor behind the vault, the opening at the end of the tunnel.

### 20.1 Foreground / Midground / Background

**Rule:** Every frame contains at minimum a midground subject and a background depth recession. Foreground elements are strongly preferred when available — they establish that the camera is physically inside the space, not observing from a safe distance.

**Foreground elements:** architectural elements (the near edge of an arch, a column shaft, a wall face close to the camera), atmospheric density (haze or dust in the near field), a period object at close range with the subject behind it.

**Background elements:** depth recession (a corridor extending away, a shaft rising above, a vault receding), atmospheric softening (haze increasing with distance), light falling off at depth.

### 20.2 Depth Revealed Through Movement

**Rule:** Camera movement that advances through space (slow push-in, forward dolly along a tunnel axis) must reveal new depth information as it moves — successive spatial layers entering and passing through the frame as the camera advances.

**Why.** A forward camera movement that does not reveal new spatial layers is moving toward a flat surface, not through a real space. Real physical depth means that new elements enter the frame as the camera advances. This revelation of depth through movement is the primary evidence of three-dimensional space in documentary video.

### 20.3 Forbidden Depth Behaviors

- **Depth-free close-up:** Extreme close-up with a flat, featureless background — no depth recession, no atmospheric haze, no spatial layer behind the subject. This reads as a product shot against a backdrop.
- **Artificial depth of field bokeh as depth substitute:** Using extreme lens blur to imply depth without structural depth layers in the frame. Blur is not depth — it is the appearance of depth without the reality.

---

## 21. Audio Policy

**Principle:** P1 Documentary Visual Philosophy, P20 Viewer Trust

**Why.** AI video generation systems produce audio as part of the generation output — ambient, atmospheric, or diegetic sound inferred from the visual content of the scene. The channel's assembly pipeline adds narration and licensed music as separate, independently edited audio layers at the assembly stage. Audio baked into generated clips by the generation system competes with these intentional layers and degrades the final audio experience. Audio baked into a generated clip cannot be reliably removed after the fact — it is part of the clip output. The audio policy must therefore be declared and enforced at the moment of generation, not corrected afterward.

### 21.1 Permitted Audio in Generated Clips

Generated clips may contain only ambient atmospheric audio consistent with the physical environment of the scene:

- Stone acoustics: natural reverb and room tone of stone interiors
- Environmental sound: distant crowd murmur (non-intelligible), water dripping on stone, wind through openings, fire crackle at distance
- Material sound: rope under tension, timber under load, stone settling, rubble displacement
- Atmospheric sound: underground air movement, outdoor wind, rainfall on exterior surfaces

### 21.2 Prohibited Audio in Generated Clips

**Music:** No generated music, instrumental scoring, rhythmic elements, or tonal atmosphere music. The licensed music track is the sole music layer in the assembly.

**Narration and speech:** No generated speech, voiceover, narration, explanation, or intelligible human voice at any volume. The channel's narration track is the sole voice layer in the assembly — it is produced and controlled separately.

**Singing and chanting:** No generated vocal performance, ceremonial chanting, crowd chanting, or sung content — even if period-plausible. These layers compete with the narration and are not under editorial control in the assembly pipeline.

**Sound effects designed to be mixed:** No generated audio that is clearly designed as a foreground sound-design element intended for mixing (sword impact, explosion, dramatic sting). The assembly does not have a sound effects mix pipeline — all foreground SFX are licensed or sourced separately.

**Modern audio signatures:** No reverb or processing effects that sound like contemporary studio production (plate reverb, gate reverb, chorus, delay effects that are audibly modern).

### 21.3 Audio Intent Declaration Requirement

**Rule:** Every generated clip must be produced against an explicit declaration of its intended audio content. That declaration must specify: the natural environmental ambience of the physical scene as the sole permitted audio; the prohibition of music in any form; the prohibition of speech, narration, and intelligible human voice at any volume; and the prohibition of any audio element not consistent with the physical conditions of the scene as described. Audio intent left undeclared is a production failure — it cannot be assumed to conform to the channel's standard.

**Why.** A generation system infers audio content from its visual description. A scene describing crowds will produce vocal content. A scene describing mechanisms will produce mechanical sound effects. A scene describing a ceremonial space will produce chanting or scored atmosphere. None of these inferences are consistent with the channel's audio policy. Intent must be declared explicitly and cannot be delegated to the generation system's inference from visual content.

---

## 22. Provider-Independent Rules

**Principle:** P20 Viewer Trust, P15 Visual Continuity

**Why.** The channel's visual identity must be maintained regardless of which AI video provider is used for a given generation. Provider choice is a cost/quality/availability decision — it must not result in visual inconsistency. These rules apply to the desired output regardless of which model produces it.

### 22.1 Universal Visual Standards

The following standards apply to every generated clip regardless of provider:

- Near-black shadow zones occupy 50–65% of frame area
- Warm key light is the primary light source
- A secondary cooler light source provides edge separation
- Surfaces show period-appropriate material specificity and age
- No CGI, illustration, or game-engine appearance
- Camera movement is physically motivated
- Atmospheric condition evidence is present
- No anachronistic elements

### 22.2 Provider Selection Philosophy

Provider selection is determined by the requirements of the specific scene against the provider's known quality and behavioral profile, subject to cost constraints. Higher-tier providers are reserved for scenes where the visual requirements (complex architectural reveals, large-scale environmental establishing, multi-element composition) exceed what lower-tier providers reliably produce.

Higher-tier investment criteria: hero/opening scenes establishing channel visual identity; architectural revelation scenes (architectural cutaway, section cut, mechanical cross-section) where compositional precision is required; final scenes where the emotional arc depends on visual quality.

---

## 23. Duration Rules

**Principle:** P18 Motion Philosophy

**Why.** Clip duration is not a provider constraint to be worked around — it is a creative unit that determines what is possible within a single clip. An 8-second clip contains a specific amount of narrative and visual information. The planning system must understand what can and cannot be communicated in 8 seconds.

### 23.1 Standard Clip Duration

**Target:** 8 seconds per clip for all scene types in all episode formats. This is the standard unit of visual time in the current pipeline.

**Rationale:** 8 seconds is sufficient to: establish a spatial condition (0–2s), develop the condition with camera movement or atmospheric shift (2–6s), and arrive at a concluding visual state (6–8s). Clips shorter than 8 seconds are too brief to establish spatial credibility. Clips longer than 8 seconds (if a future provider supports them) require their temporal structure to be extended accordingly with proportionate narrative development.

### 23.2 Temporal Structure

**Rule:** Every 8-second clip must be structured as two 4-second phases:

- **Phase 1 (0–4s):** Establishes the spatial, material, and lighting condition of the scene. The viewer should understand what space they are in and what the primary subject is by 4 seconds.
- **Phase 2 (4–8s):** Develops, shifts, or resolves the condition established in Phase 1. A camera movement that began in Phase 1 arrives at its destination. A revealed depth layer enters the frame. The light condition changes. The relationship between foreground and background shifts.

**The physical change test:** The visible condition at 4s must differ from the visible condition at 0s in a physical, observable way. The visible condition at 8s must differ from the condition at 4s in a physical, observable way. Two identical-condition frames separated by 4 seconds of camera drift do not satisfy this requirement.

---

## 24. Coverage Rules

**Principle:** P18 Motion Philosophy

**Why.** A video episode requires that every second of narration has a corresponding visual. When raw clip footage covers fewer than 50% of the narration duration, the assembly pipeline must extend clips through holds (frozen frames). Holds destroy the motion that makes clips credible as footage — a frozen frame is visually indistinguishable from a still image. Coverage is therefore a quality requirement, not only a technical one.

### 24.1 Minimum Coverage Ratio

**Rule:** The total raw clip duration (sum of clip durations for all main episode scenes, before any holds or loops) must be at least 50% of the total main episode narration duration.

**Calculation:** Raw coverage ratio = (total clip seconds) ÷ (total narration seconds). This ratio must be ≥ 0.50 before the assembly stage.

**Example:** A main episode with 600 seconds of narration requires a minimum of 300 seconds of raw clip footage (38 clips at 8 seconds per clip).

### 24.2 Maximum Hold Duration

**Rule:** No single clip may be held (displayed beyond its natural duration) for more than 16 seconds of narration coverage. A hold exceeding 16 seconds on an 8-second clip produces a visually frozen frame that is perceptible as a production failure to attentive viewers.

**Why 16 seconds.** An 8-second clip shown for 16 seconds has been displayed for 2× its natural duration. The viewer has seen all the motion the clip contains and is now watching a frozen state. Holds of up to 16 seconds are acceptable because the camera's final position can read as a deliberate editorial hold. Holds beyond 16 seconds read as a missing clip.

### 24.3 Beat Duration Analysis

**Rule:** During scene planning, every narration beat must have its duration estimated. Beats with duration > 12 seconds require planning for either: two clips (primary + secondary visual); or an explicit loop annotation indicating that the clip is designed for loop playback without visible seam.

**Threshold rationale:** A 12-second beat is best served by 1.5× clip duration (12s ÷ 8s). A single 8-second clip held to 12 seconds is within the maximum hold tolerance. A beat longer than 12 seconds that receives only one clip will require a hold that approaches or exceeds the 16-second maximum.

---

## 25. Clip Planning Rules

**Principle:** P18 Motion Philosophy, P6 Scale Perception

**Why.** Visual planning cannot be decoupled from narration duration. The planning stage must know both what to show and how long it needs to show it. A planning system that assigns one clip per beat without considering beat duration is not planning coverage — it is distributing prompts.

### 25.1 One-Scene-Per-Beat as Default

One scene per narration beat is the correct default when the beat duration is 6–12 seconds. This range accommodates an 8-second clip with a natural hold before or after the clip.

### 25.2 Multi-Clip Beats

Beats with estimated duration > 12 seconds require a second clip planned alongside the primary. The second clip must:

- Show a different spatial layer, framing, or aspect of the same subject
- Not repeat the same camera movement type as the primary
- Provide visual continuity (same period, palette, and atmospheric condition)

### 25.3 Scene Planning Input Requirement

**Rule:** Scene planning input must include estimated narration duration for each beat. This duration drives coverage calculation and multi-clip decisions. Scene plans submitted without beat duration data cannot be validated for coverage compliance.

---

## 26. Repair Philosophy

**Principle:** P20 Viewer Trust

**Why.** When a clip generation fails or produces output that does not meet the style system's standards, the repair strategy determines whether the episode's visual quality is recoverable. Repair is a fallback, not a design approach. The goal of repair is to restore coverage and quality, not to introduce new visual approaches that were not planned.

### 26.1 Repair Triggers

A clip requires repair when:
- Generation failed (provider returned no output or an error)
- The generated clip fails the photographic test (§18.4)
- The generated clip fails the documentary register test (§5)
- The generated clip contains prohibited audio (§21.2)
- The generated clip contains anachronistic elements (§17.1)

### 26.2 Repair Strategy

**First attempt:** Regenerate using the fallback prompt (a simplified version of the primary prompt that reduces compositional complexity while maintaining the same period, palette, and atmospheric requirements).

**Second attempt:** Regenerate using a modified version of the primary prompt that reduces the requirements causing the failure — specifically: reduce compositional complexity (fewer simultaneous elements), simplify the lighting setup (single warm key only), remove secondary subject elements. Maintain the core narrative function of the shot.

**Third attempt:** Replace with a period-consistent establishing shot that covers the same narration beat without the specific compositional requirement of the original. Document the substitution in the visual integrity log.

**If all attempts fail:** Flag for operator review. Do not use a hold to cover a beat whose entire visual is a failed generation — this produces a freeze-frame over the most important narration beats, which are typically the ones that triggered the most complex visual requirements.

---

## 27. Validation Rules

**Principle:** P20 Viewer Trust

**Why.** Validation is the pipeline mechanism that ensures the style system's rules are honored in practice. Without validation, the rules are aspirational rather than enforced. Validation occurs at two stages: pre-generation (checking the plan and prompt before any provider spend) and post-generation (checking the actual clip output against the standard).

### 27.1 Pre-Generation Validation Requirements

The following must be validated before any generation credit is spent:

| Check | Rule Reference | Failure Consequence |
|---|---|---|
| No CGI / illustration language in prompt | §18 | Reject prompt; rewrite required |
| No prohibited audio language in prompt | §21 | Reject prompt; add audio restriction |
| Warm key light present in every prompt | §8.2 | Reject prompt; lighting revision required |
| Secondary cool source present in every prompt | §8.3 | Reject prompt; add secondary source |
| Environmental context present (non-void) | §15.1 | Reject prompt; add environmental detail |
| No split-screen / composited language in prompt | §11.3 | Reject prompt; single-frame revision required |
| No prohibited color language (electric blue, neon) | §7.4 | Reject prompt; palette correction required |
| No anachronistic elements described in prompt | §17.1 | Reject prompt; period revision required |
| No frontal face described in prompt | §17.3 | Reject prompt; substitution required |
| Camera movement is narratively motivated | §11.2 | Flag for review |
| Beat duration estimated and coverage math computed | §25.3 | Block generation if coverage ratio < 0.50 |
| Audio instruction present in prompt | §21.3 | Reject prompt; add audio instruction |

### 27.2 Post-Generation Validation Requirements

The following must be checked on each generated clip:

| Check | Rule Reference | Failure Consequence |
|---|---|---|
| Does not look CGI or rendered | §18.1 | Trigger repair (§26) |
| Warm lighting visible and dominant | §8.2 | Trigger repair |
| Secondary cool source visible | §8.3 | Flag for review |
| No anachronistic elements visible | §17.1 | Trigger repair |
| No frontal faces visible | §17.3 | Trigger repair |
| Environmental context visible (non-void) | §15.1 | Flag for review |
| No split-screen or composite visual | §11.3 | Trigger repair |
| Camera motion present (except permitted holds) | §11.4 | Flag for review |
| Atmospheric evidence present | §10.1 | Flag for review |
| Audio: no music, speech, or narration | §21.2 | Trigger repair or audio processing |
| No generated text / watermarks in frame | §17 | Trigger repair |

---

## 28. Failure Library

This section documents the documented failure modes in the channel's video production history. Each entry identifies the visual signature, root cause, and the style system rule that prevents recurrence.

### 28.1 CGI Appearance Failure

**Visual signature:** Clips look rendered rather than photographed. Surfaces are too perfect. Lighting is too even or too idealized. The space feels computer-generated.

**Root cause:** Absence of material specificity instructions. Absence of atmospheric density. Lighting described in terms of mood rather than physical motivated sources.

**Prevention:** §16 Material Authenticity (specific surface observations required), §10 Atmosphere (atmospheric evidence required), §8 Lighting System (motivated physical sources required), §18.1 CGI Prohibition.

### 28.2 Split-Screen Default Failure

**Visual signature:** When a scene requires two spatial zones to coexist (arena above, tunnel below), the generated clip splits the frame into two halves showing each zone independently. The two zones have separate visual treatments that do not coexist physically.

**Root cause:** The planning stage identified two spatial elements but did not specify how they relate in a single physical frame. The generation model defaulted to the simplest two-element composition: left/right or top/bottom split.

**Prevention:** §11.3 Forbidden Camera Behaviors (split camera paths prohibited), §13.3 Layers of Depth (depth layering, not lateral splitting), §11.2 Camera Language (narrative function determines physical movement).

### 28.3 Freeze-Hold Failure

**Visual signature:** During assembly, a clip is held (displayed as a static frame) for 20–30 seconds because the narration beat is significantly longer than the 8-second clip. The visual is effectively a still image over a long section of narration.

**Root cause:** Scene planning assigned one 8-second clip per beat without calculating the beat's narration duration. The assembly stage discovered the coverage gap and filled it with a hold.

**Prevention:** §24 Coverage Rules (minimum 50% raw coverage ratio), §25 Clip Planning Rules (beat duration analysis required), §11.4 Maximum Hold Duration (16 second limit).

### 28.4 Audio Contamination Failure

**Visual signature:** The assembled episode has competing audio layers — the licensed music track, the channel's narration track, and baked-in audio from one or more generated clips. The result is an unintelligible or discordant audio mix.

**Root cause:** No audio policy in the generation prompt. Provider generated ambient audio that is too prominent or that contains musical or speech-like elements.

**Prevention:** §21 Audio Policy (ambient/SFX only permitted), §21.3 Audio instruction required in every prompt.

### 28.5 Atmospheric Void Failure

**Visual signature:** The clip shows a visually dramatic subject against a featureless near-black background with no atmospheric evidence of the surrounding environment. The space looks like a void with a subject placed in it.

**Root cause:** Emphasis on lighting and subject description without corresponding atmospheric and environmental context requirements.

**Prevention:** §10 Atmosphere (atmospheric evidence required in enclosed and exterior spaces), §15 Environmental Storytelling (environmental context required), §13.2 Frame Filling (near-black void maximum 65% of frame).

### 28.6 Warm Monochrome Failure

**Visual signature:** All three A/B/C thumbnail candidates or multiple consecutive video scenes share exactly the same warm amber color temperature with no cool secondary source anywhere. The result is a palette that lacks visual tension and reads as decorative amber rather than documentary evidence.

**Root cause:** Cool secondary source not required or specified. Planning stage optimized for warmth and atmosphere without requiring the temperature contrast that gives the palette visual dimension.

**Prevention:** §8.3 Secondary Light Source (cool secondary required in every scene), §7.2 Warm/Cool Balance (maximum 65% warm-illuminated zone).

---

## 29. Rule Priority

**Principle:** P20 Viewer Trust

When two rules produce conflicting requirements for a specific scene, resolve using this priority order:

1. **Viewer Trust rules** (§18 Realism, §5 Documentary Identity, §17 Historical Authenticity) — a rule that violates perceived realism or historical credibility supersedes any other consideration
2. **Audio Policy** (§21) — baked audio contamination is not recoverable in assembly; prevention supersedes any other requirement
3. **Coverage Requirements** (§24) — insufficient coverage produces freeze-hold failures that damage the viewing experience across every affected scene
4. **Visual Hierarchy rules** (§13, §14) — competing visual elements destroy the curiosity gap mechanism
5. **All other rules** — apply in parallel; conflicts between them are resolved by which option better serves the curiosity gap mechanism (§4)

---

## 30. Traceability Requirements

**Rule:** Every rule in every downstream runtime module must be traceable to a section of this document. The traceability chain is:

```
visual_design_principles.md (Principle: P_)
    → video_style_system.md (Section: §_)
        → Runtime module (rule implementation)
```

**Traceability format for downstream files:**
```
# STYLE_SYSTEM_SYNCED: video_style_system.md
# CANONICAL_RULE: §<section>.<subsection>
```

A runtime rule that cannot cite a section of this document is architectural drift and must be either traced to an existing section, moved into this document as a new section, or removed.

---

## 31. Versioning

**Version:** 1.2

**This document may be updated when:**
- New failure modes are identified and documented (adds to §28)
- New provider capabilities change what is achievable (adds to §22, §23, §24)
- New episode production reveals that a rule does not produce the intended result (updates the relevant rule with evidence)
- A principle in `visual_design_principles.md` is added or revised (cascades into all rules that cite that principle)

**This document may NOT be updated to:**
- Accommodate a specific implementation shortcut that does not serve the principles
- Reverse a prohibition because it is inconvenient for a current runtime module
- Add a rule that originates in a runtime module rather than in a principle

**Version history must be maintained at the bottom of this document.**

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-07 | Initial canonical specification. 31 sections. Derived from visual_design_principles.md and brand board. |
| 1.1 | 2026-07-07 | Added §32 Traceability Table. Maps all major rules to originating principles, evidence sources, and future runtime owners. |
| 1.2 | 2026-07-07 | Phase 0 canonical stabilization. §11.2: one movement type per scene clarification. §11.3: dutch_tilt added to forbidden list; handheld restriction sharpened. §19.4: Camera Energy Progression added. §21: Why rewritten as provider-independent; §21.3 rewritten as Audio Intent Declaration Requirement (cinematic intent only, no provider strings). §33: Scene Narrative Focus (one primary narrative focus per scene; one primary visual action per scene). §34: Cross-Episode Visual Continuity (episode-level and channel-level visual parameter consistency). §32 Traceability Table renumbered to §35. |

---

## 33. Scene Narrative Focus

**Principle:** P12 Cinematic Restraint, P4 Curiosity Generation

**Why.** A single generated clip contains 8 seconds of visual content. In that time, the camera establishes one spatial condition, moves within it, and arrives at a concluding visual state. This is the capacity of one clip — one claim, one environment, one subject relationship. A clip planned to serve two narrative purposes simultaneously exceeds this capacity and typically fails both: the camera cannot commit to a movement that serves two different functions, and the viewer cannot determine which claim is primary.

### 33.1 One Primary Narrative Focus Per Scene

**Rule:** Every planned scene must have exactly one primary narrative focus. The primary focus is the single claim the scene makes: this space has this character; this mechanism operates in this way; this material has this physical property; this architectural element occupies this scale. The planning record must identify this focus in a single sentence. If the focus requires two sentences, it requires two scenes.

**Secondary environmental elements are permitted and encouraged** — provided they support rather than compete with the primary focus. Atmospheric particulate moving through a light beam supports a scene whose focus is the quality of torchlight in a stone interior. The same atmospheric particulate in a scene whose focus is a mechanism in operation is a competing distraction if it draws the eye away from the mechanism.

**The support test:** if a secondary element were removed from the scene, would the primary focus become clearer? If yes, the secondary element is competing with the focus. If no, it is supporting it.

### 33.2 One Primary Visual Action Per Scene

**Rule:** Every scene contains one primary visual action — the principal physical event the viewer tracks. This is distinct from the narrative focus (what the scene claims) and from the camera movement (how the camera relates to the scene). The visual action is what changes within the frame during the 8 seconds: water percolating through a crack, rope under tension vibrating, torch smoke rising into a vault, a shadow edge advancing across a surface as a light source shifts.

Secondary environmental motion (§19.3) is not the primary visual action — it is ambient evidence of physical reality. The primary visual action is the foreground event that holds the viewer's attention and confirms that real time is passing within a real physical space.

**One primary visual action does not mean a static frame.** It means one foreground event that the viewer actively tracks, surrounded by ambient environmental motion that confirms the space is alive.

---

## 34. Cross-Episode Visual Continuity

**Principle:** P15 Visual Continuity, P4 Curiosity Generation

**Why.** The channel's visual identity is not a property of individual frames — it is the product of a consistent visual register sustained across all clips in an episode and across all episodes in the channel. A viewer watching an episode whose early scenes are warm-amber torch-lit interiors and whose late scenes drift into cool-grey-dominant exterior frames — without a motivated narrative transition — has experienced an inadvertent visual discontinuity. The visual register must be consistent unless a deliberate editorial choice (location change, time-of-day shift, structural section break) justifies a visual register transition.

### 34.1 Episode-Level Visual Parameters

The following visual parameters must be established at the episode planning stage and held consistently across all clips sharing the same setting, time of day, and narrative context:

**Dominant color temperature:** the character of the primary key light (amber torch interior, Mediterranean midday exterior, dawn overcast, archaeological site excavation daylight) must be consistent across scenes set in the same physical environment and time. Scenes in different environments or times may carry a different temperature — but the transition between them must be editorially motivated and visible to the viewer.

**Atmospheric register:** the type, density, and behavior of atmospheric evidence (underground suspended particulate, exterior dust and heat shimmer, underwater ambient diffusion) must be consistent with the physical conditions of the episode's primary setting across all scenes within that setting.

**Lighting character:** the quality of the key light — hard directional torch, focused shaft of exterior sunlight, diffuse cave ambient — defines the episode's visual texture within its primary environment. This character must remain consistent within scenes sharing the same physical setting.

### 34.2 Permitted Variation Between Scenes

The following may differ between scenes within the same episode without constituting visual discontinuity:

- Camera movement type — each scene selects the type appropriate to its narrative function (§11.2)
- Shot scale — each scene selects the scale appropriate to its subject and narrative function (§12.1)
- Primary subject — the specific object, architectural element, or mechanism that is the focus of each scene
- Camera energy within its permitted arc (§19.4)
- Secondary atmospheric elements within the established atmospheric register

### 34.3 Channel-Level Visual Consistency

Across episodes, the channel identity palette (§7.1), the defaults of the lighting system (§8.2, §8.3), the contrast character (§9.3), and the atmospheric register must remain consistent regardless of episode topic or historical period. Period-specific palette variations (§7.3) operate within these constraints — they narrow or warm the channel palette for a specific context but do not replace it.

**The channel consistency test:** a single frame extracted from any episode of the channel, shown without episode context, must be immediately recognizable as belonging to The Hypogeum Chronicles. If it could belong to any other historical documentary channel, the visual identity has drifted.

---

## 35. Traceability Table

This table maps every major rule in the style system to: its originating principle in `visual_design_principles.md`, the evidence source that confirms the rule's necessity, and the future runtime module that will enforce it.

**Evidence key:**
- **Brand Board** — `hypogeum/brand/references/brand_board.jpg` (primary visual specification input)
- **V0 Audit** — video pipeline audit findings from 2026-07-07 (documented gaps and failure modes)
- **CPA** — `cinematic_prompt_architecture.md` (9-layer prompt technique reference)
- **VLE** — `visual_language_engine.md` (14 presentation method reference)
- **ep_reg_vi_colosseum** — reference implementation; evidence that a rule produces desired output, not a specification source
- **Failure Library §28** — documented production failure modes

**Runtime Owner key:**
- **VBG** — Video Brand Guardian (semantic enforcement, pre-generation pass)
- **VSV** — Video Style Validator (structural/parametric checks)
- **SP** — Scene Planner
- **PP** — Presentation Planner
- **SHP** — Shot Planner
- **PW** — Prompt Writer
- **PROV** — Provider Payload (API parameter layer)
- **RQA** — Render QA (post-generation)

---

### 35.1 Color System

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Deep void shadow: #080808–#141414 | §7.1 | P8 Color Psychology | Brand Board | PW, VSV |
| Warm dark mid: #1F1A14–#2E2519 | §7.1 | P8 Color Psychology | Brand Board | PW, VSV |
| Bronze / aged metal: #836333 | §7.1 | P8 Color Psychology | Brand Board | PW |
| Amber / gold key light: #C4A642–#D4AF37 | §7.1 | P8 Color Psychology | Brand Board | PW |
| Cool atmospheric accent: #4A6080–#6A8CB0 | §7.1 | P8 Color Psychology | Brand Board | PW |
| Warm-illuminated zones 35–50% of frame | §7.2 | P8, P15 | Brand Board, V0 Audit (no video hex table existed) | PW, VSV |
| Shadow zones 50–65% of frame | §7.2 | P8, P15 | Brand Board | PW, VSV |
| Cool accent maximum 15–20% of frame | §7.2 | P8 | Brand Board | PW, VSV |
| No electric blue, neon, green, pink, purple dominant | §7.4 | P8 | V0 Audit, Failure Library §28 | VBG, PW |
| No hyper-saturated colors | §7.4 | P8 | V0 Audit | VBG |
| No uniform warm orange (Instagram filter) | §7.4 | P8 | V0 Audit | VBG |

---

### 35.2 Lighting System

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Minimum two light sources per scene | §8.1 | P7 Lighting Psychology | Brand Board | SHP, PW |
| Maximum three light sources per scene | §8.1 | P7 | Brand Board | SHP, PW |
| Warm key light: amber/torch 2,700–3,200K | §8.2 | P7 | Brand Board, ep_reg_vi_colosseum | SHP, PW |
| Key light direction: 30–60° lateral, 20–40° elevated | §8.2 | P7 | Brand Board | SHP, PW |
| Flat frontal lighting forbidden | §8.2 | P7 | V0 Audit, Failure Library §28.6 | VBG, PW |
| Key light falloff sharp; illuminated zone ≤ 35–45% of frame | §8.2 | P7 | Brand Board | PW, VSV |
| Secondary source 200–500K cooler than key | §8.3 | P7 | Brand Board | SHP, PW |
| Secondary: no electric blue, no bioluminescent blue | §8.3 | P7 | V0 Audit | VBG, PW |
| Secondary source never competing for dominance on primary subject | §8.3 | P7 | Brand Board | SHP, PW |
| Forbidden: ambient, ring, softbox, fluorescent | §8.4 | P7 | V0 Audit | VBG, PW |
| Underwater: warm torch dominant; teal subordinate only | §8.5 | P7 | Brand Board | PW |

---

### 35.3 Contrast and Atmosphere

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Black point true near-black; shadows not lifted to grey | §9.1 | P7 | Brand Board | VBG, VSV |
| Highlight peaks warm amber/gold; not neutral white | §9.2 | P7 | Brand Board | PW |
| Key-lit zone ≤ 35–45% of frame | §9.2 | P7 | Brand Board | VSV |
| Contrast ratio target 8:1 to 12:1 | §9.3 | P7 | Brand Board | VSV, RQA |
| Underground / enclosed: suspended particulate, combustion haze, or moisture evidence required | §10.1 | P19 Atmosphere Philosophy | ep_reg_vi_colosseum, V0 Audit | PW |
| Exterior: airborne dust, atmospheric haze, or environmental particulate required | §10.1 | P19 | ep_reg_vi_colosseum | PW |
| Atmospheric behavior must be physically motivated (smoke drifts up, dust traces light beam) | §10.2 | P19 | ep_reg_vi_colosseum | PW, VBG |
| No uniform mist; no smoke independent of light | §10.3 | P19 | Failure Library §28.5 | VBG, PW |

---

### 35.4 Camera Language

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Standard camera height 5–15° downward | §11.1 | P11 Human Attention Guidance | CPA, ep_reg_vi_colosseum | SHP, PW |
| Low angle (15–45° below midline) for scale revelation | §11.1 | P11 | CPA | SHP, PW |
| High angle only for spatial comprehension; not default | §11.1 | P11 | CPA | SHP, PW |
| Every camera movement must serve a specific narrative function | §11.2 | P18 Motion Philosophy | CPA | SHP, PW |
| Discovering → slow push-in | §11.2 | P18 | CPA | SHP, PW |
| Surveying → lateral dolly / slow pan | §11.2 | P18 | CPA | SHP, PW |
| No static hold as primary movement for non-establishing scenes | §11.3 | P18 | V0 Audit (s33 QA anomaly), Failure Library §28.3 | VBG, PW |
| No unmotivated drift | §11.3 | P18 | V0 Audit | VBG, PW |
| No split or composite camera paths | §11.3 | P18 | Failure Library §28.2 | VBG, PW |
| Maximum hold: 16 seconds per clip | §11.4 | P18 | V0 Audit (29.5s max hold observed in ep_reg_vi_colosseum) | SP, RQA |

---

### 35.5 Lens Language and Composition

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Nine permitted shot scales (ECU through overhead and macro) | §12.1 | P12 Cinematic Restraint, P4 Curiosity Generation | CPA | SHP, PW |
| Depth of field: wide to medium default; shallow only when functionally required | §12.2 | P12 | Brand Board | PW, VBG |
| No extreme shallow focus as default beautification | §12.2 | P12 | V0 Audit | VBG |
| Primary subject: right 40–60% of frame | §13.1 | P10, P11 | Brand Board | PP, PW |
| Frame must be filled; near-black max 65% | §13.2 | P10 | Brand Board, Failure Library §28.5 | PW, VSV |
| Every frame: minimum two spatial layers (midground + background) | §13.3 | P9 Spatial Storytelling, P10 | CPA | PP, PW |
| One primary subject per frame; competing equal-weight elements forbidden | §13.4 | P10, P16 | Brand Board | PP, PW, VBG |

---

### 35.6 Subject Scale and Environment

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Monumental: subject 45–65% of vertical height | §14.1 | P6 Scale Perception | Brand Board, ep_reg_vi_colosseum | PP, PW |
| Scale Contrast: subject 5–15% of frame height | §14.1 | P6 | Brand Board, ep_reg_vi_colosseum | PP, PW |
| Forbidden zone: 20–40% of frame height (product-shot zone) | §14.1 | P6, P10 | V0 Audit | VBG, PP |
| Scale anchor required in every main episode scene | §14.2 | P6 | ep_reg_vi_colosseum | PP, PW |
| Every subject in a plausible ancient physical environment | §15.1 | P9, P14 Environmental Storytelling | V0 Audit, Failure Library §28.5 | PW, VBG |
| Pure void backgrounds not permitted for main episode content | §15.1 | P14 | V0 Audit | VBG, PW |
| Environment must answer: where, what done here, who was here | §15.2 | P9, P14 | ep_reg_vi_colosseum | PP, PW |
| No generic ancient space (vault with no construction period) | §15.3 | P14 | Failure Library §28.5 | VBG |
| No pristine environment (ancient spaces are not clean) | §15.3 | P14 | ep_reg_vi_colosseum | VBG, PW |

---

### 35.7 Material Authenticity

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Every scene: at least one material observation specific to period, environment, and lighting | §16.1 | P13 Material Authenticity | ep_reg_vi_colosseum | PW |
| Imperial Rome material specifics (travertine, tuff, opus incertum, fired brick, timber, hemp) | §16.1 | P13 | ep_reg_vi_colosseum, V0 Audit | PW |
| Materials must show age and use history | §16.2 | P13 | ep_reg_vi_colosseum | PW, VBG |
| Age evidence indicators (mineral staining, wear, biological, structural) | §16.2 | P13 | ep_reg_vi_colosseum | PW, VBG |

---

### 35.8 Historical Authenticity

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| No modern light sources (electric, fluorescent, LED) | §17.1 | P2 Historical Credibility | V0 Audit | VBG, PW |
| No modern construction materials (steel, modern concrete, plate glass) | §17.1 | P2 | V0 Audit | VBG, PW |
| No contemporary human elements (modern clothing, tools, post-period artifacts) | §17.1 | P2 | V0 Audit | VBG, PW |
| Conservative interpretation for disputed historical evidence | §17.2 | P2 | — | PP, PW |
| Substitutions flagged in visual_integrity_log | §17.2 | P2, P20 | — | RQA |
| No frontal face shots (living, ancient, sculpted, fresco, mosaic) | §17.3 | P2, P4 | V0 Audit (no E-TH05 video equivalent) | VBG, PW, RQA |

---

### 35.9 Realism Rules

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| CGI appearance prohibition: no uniform surfaces, no even illumination, no uniform shader | §18.1 | P5 Perceived Realism, P1, P20 | V0 Audit (no CGI blocking for video existed), Failure Library §28.1 | VBG, RQA |
| Illustration / concept-art prohibition: no stylized rendering, no painterly texture | §18.2 | P5 | V0 Audit | VBG, RQA |
| Game-engine prohibition: no tiled normals, no baked AO, no hyper-precise reflections | §18.3 | P5 | V0 Audit | VBG, RQA |
| Photographic test: must pass "could a filmmaker have photographed this?" | §18.4 | P5, P1, P20 | V0 Audit | VBG, RQA |

---

### 35.10 Motion Language

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Static camera permitted only when subject contains all required motion | §19.1 | P18, P12 | V0 Audit (s33 freeze anomaly) | SHP, PW |
| Fully locked camera with no ambient motion forbidden beyond 8 seconds | §19.1 | P18 | V0 Audit | SHP, PW, RQA |
| Every 8-second clip: physical change between 0–4s and 4–8s | §19.2 | P18 | V0 Audit | PW, RQA |
| Physical change must be observable: position, light state, subject state, or revealed information | §19.2 | P18 | CPA, ep_reg_vi_colosseum | PW |
| Every clip: at least one secondary motion element (smoke, dust, water, fabric, sand) | §19.3 | P18 | ep_reg_vi_colosseum | PW |
| Camera advancing through space must reveal new depth layers | §20.2 | P9 | CPA | SHP, PW |

---

### 35.11 Audio Policy

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Permitted audio: ambient atmospheric only (stone acoustics, environmental sound, material sound) | §21.1 | P1, P20 | V0 Audit (no audio policy existed anywhere in pipeline) | PW, PROV |
| Prohibited: music, instrumental scoring, rhythmic elements | §21.2 | P1, P20 | V0 Audit, Failure Library §28.4 | PW, PROV |
| Prohibited: speech, narration, voiceover, any intelligible human voice | §21.2 | P1, P20 | V0 Audit | PW, PROV |
| Prohibited: singing, chanting, vocal performance | §21.2 | P1, P20 | V0 Audit | PW, PROV |
| Prohibited: SFX designed for mixing (foreground sound design elements) | §21.2 | P1, P20 | V0 Audit | PW, PROV |
| Prohibited: modern audio signatures (plate reverb, gate, chorus, delay effects) | §21.2 | P1, P20 | V0 Audit | PW |
| Audio intent declaration required in every generation request | §21.3 | P1, P20 | V0 Audit (generation systems produce audio by default; no audio policy existed anywhere in pipeline) | PW |

---

### 35.12 Duration and Coverage

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Standard clip duration: 8 seconds | §23.1 | P18 | V0 Audit (8-only policy; legacy files said 5 or 8) | PROV |
| Temporal structure: Phase 1 (0–4s establish) + Phase 2 (4–8s develop) | §23.2 | P18 | CPA, ep_reg_vi_colosseum | PW |
| Minimum coverage ratio ≥ 0.50 (raw clip seconds ÷ narration seconds) | §24.1 | P18 | V0 Audit (ep_reg_vi_colosseum: 264s ÷ 677s = 38.9%; below threshold) | SP |
| Maximum hold: 16 seconds per clip | §24.2 | P18 | V0 Audit (29.5s max hold observed in ep_reg_vi_colosseum) | SP, RQA |
| Beat duration > 12s requires second clip or explicit loop annotation | §24.3 | P18 | V0 Audit, Failure Library §28.3 | SP |
| Beat duration estimated during scene planning; scene plans without duration data are not coverage-valid | §25.3 | P18 | V0 Audit | SP |
| Multi-clip beats: second clip must differ in spatial layer, framing, or aspect | §25.2 | P18 | V0 Audit | SP, PP |

---

### 35.13 Validation and Repair

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Pre-generation: no CGI / illustration language in prompt | §27.1 | P5, P20 | V0 Audit | VSV, PW |
| Pre-generation: audio instruction present | §27.1 | P1, P20 | V0 Audit | VSV, PW |
| Pre-generation: warm key light present | §27.1 | P7 | Brand Board | VSV, PW |
| Pre-generation: secondary cool source present | §27.1 | P7 | Brand Board | VSV, PW |
| Pre-generation: environmental context present (non-void) | §27.1 | P14 | Failure Library §28.5 | VSV, PW |
| Pre-generation: no split-screen language | §27.1 | P11 | Failure Library §28.2 | VSV, PW |
| Pre-generation: no prohibited color language | §27.1 | P8 | Brand Board | VSV, PW |
| Pre-generation: no anachronistic elements | §27.1 | P2 | V0 Audit | VSV, PW |
| Pre-generation: no frontal face | §27.1 | P2 | V0 Audit | VSV, PW |
| Pre-generation: coverage ratio ≥ 0.50 enforced before generation begins | §27.1 | P18 | V0 Audit | SP, VSV |
| Post-generation: does not look CGI or rendered | §27.2 | P5 | Failure Library §28.1 | RQA |
| Post-generation: warm lighting visible and dominant | §27.2 | P7 | Brand Board | RQA |
| Post-generation: no anachronistic elements visible | §27.2 | P2 | V0 Audit | RQA |
| Post-generation: no frontal faces visible | §27.2 | P2 | V0 Audit | RQA |
| Post-generation: camera motion present (except permitted holds) | §27.2 | P18 | V0 Audit | RQA |
| Post-generation: atmospheric evidence present | §27.2 | P19 | ep_reg_vi_colosseum | RQA |
| Post-generation: no music, speech, narration in audio | §27.2 | P1, P20 | V0 Audit | RQA |
| Repair: failed clip regenerated up to 3 attempts before operator escalation | §26.2 | P20 | V0 Audit | RQA |
| Repair: holds must not cover beats whose entire visual is a failed generation | §26.2 | P20 | Failure Library §28.3 | RQA |

---

### 35.14 Cross-Cutting

| Rule | §Ref | Principle | Evidence | Future Runtime Owner |
|---|---|---|---|---|
| Rule priority order: Viewer Trust > Audio > Coverage > Visual Hierarchy > all others | §29 | P20 | V0 Audit | VBG, SP, RQA |
| Provider capability definitions in scene planner must reflect actual deployed models and costs | §22.1 | P15, P20 | V0 Audit (capability metadata was stale at time of audit; planning LLM operated on incorrect cost and model assumptions) | SP, PROV |
| Higher-tier provider only for hero/architectural/final scenes | §22.2 | P20 | V0 Audit | SP, PROV |
| SDP camera_movement and A4 camera_strategy conflict resolved by: A4 camera_strategy takes precedence | §11.2 | P18 | V0 Audit (no tiebreaker rule existed; Stage B silently honored A4) | SHP |
| Every downstream runtime rule must cite §_section in this document | §30 | P20 | — | VBG, VSV, SP, PP, SHP, PW, PROV, RQA |
