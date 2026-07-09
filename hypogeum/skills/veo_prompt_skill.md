# Veo Prompt Skill
## Translation Reference — Video Style System → Veo Generation Language

**Document version:** 1.0
**Created:** 2026-07-07
**Hierarchy position:** Video Style System → **Veo Prompt Skill** → Cinematic Prompt Architecture → Video Brand Guardian → Runtime
**Parent document:** `hypogeum/brand/video_style_system.md`
**Downstream document:** `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md`

---

## 1. Purpose

This document teaches the system how Veo interprets prompt language. It does not define cinematic rules. It does not make creative decisions. Every cinematic decision belongs to the Video Style System. This document exists solely to translate those decisions into the vocabulary and structure that Veo reliably processes.

**What this document is:**

A provider-specific translation reference. When the Video Style System says "warm directional key light from a period-motivated source," this document explains how that canonical requirement becomes prompt language that Veo acts on correctly.

**What this document is not:**

- It is not the canonical authority. `video_style_system.md` is the authority.
- It is not a runtime module. It does not execute anything.
- It is not a validator. It does not check compliance.
- It is not a prompt template. It does not produce prompts.
- It is not the Cinematic Prompt Architecture. The CPA derives its layer structure and vocabulary from this document — this document explains why those choices are correct for Veo.

**The translation chain:**

```
Video Style System (what the output must be)
        ↓
Veo Prompt Skill (how Veo processes language)
        ↓
Cinematic Prompt Architecture (the assembled prompt structure)
        ↓
Generated clip (the output)
```

Every future version of the CPA should trace its vocabulary, ordering decisions, and structural choices back to a specific behavior documented here. If the CPA includes a convention that cannot be traced to a behavior in this document, that convention is unverified.

---

## 2. Provider Behaviour

This section documents verified Veo prompt behavior only. Behaviors marked **[requires verification]** have been observed in practice or reported in the Veo Tools audit but have not been confirmed against official Veo documentation. Do not build hard architectural rules on behaviors marked as requiring verification without first confirming them.

### 2.1 Prompt Element Weighting

**Verified:** Veo weights elements described earlier in the prompt more heavily than elements described later. Subject, action, and primary visual conditions stated at the beginning of the prompt exert stronger influence over the output than equivalent elements described at the end.

**Implication:** The most important elements — subject declaration, shot scale, lighting character — must appear early. Negative constraints and technical tags are least influential and are correctly placed at the end.

**[Requires verification]:** The exact gradient of this weighting (whether it is a steep decay or a gradual one) has not been measured against a controlled set.

### 2.2 Camera Movement Interpretation

**Verified:** Veo interprets cinematic camera movement verbs and produces corresponding output. The following verbs are reliably processed:

- `slow push-in` / `controlled slow push in` — camera advances toward the subject
- `pull back` / `controlled pull-back` — camera retreats from the subject
- `slow pan` — camera rotates horizontally on its axis
- `lateral tracking` / `lateral dolly` — camera moves sideways parallel to the subject
- `orbital arc` / `slow orbit` — camera arcs around the subject
- `tilt` / `slow tilt` — camera rotates vertically on its axis
- `overhead descent` — camera descends from above
- `vertical ascent` — camera rises upward
- `slow drift` — a low-energy floating motion, appropriate for wide/establishing shots

**Verified:** Speed qualifiers modify the pace of the movement. `controlled slow` produces the most deliberate, intentional motion. `deliberate` and `measured` are slightly more energetic. `urgent` and `rapid` produce faster motion. The speed qualifier must accompany the movement verb — movement without a speed qualifier produces unpredictable pace.

**Verified:** Veo cannot reliably execute two simultaneous movement types within a single clip. When given stacked movement instructions ("dolly forward while panning right"), the model tends to favor one movement and diminish or eliminate the other. The output is inconsistent across generations. Single movement type per prompt is the verified reliable approach.

**[Requires verification]:** Whether all camera verb synonyms produce equivalent output or whether Veo has preferred canonical vocabulary for each movement type.

### 2.3 Motion Interpretation

**Verified:** Motion described as a physical phenomenon is interpreted more precisely than motion described as a mood or energy. "Torch smoke drifting upward toward the vault ceiling, accumulating against the stone" produces specific atmospheric motion behavior. "A mysterious atmospheric mood" does not.

**Verified:** Describing secondary motion elements (dust particles in a light beam, water surface disturbed, rope under tension vibrating) produces those elements in the output. These secondary motion elements do not require the camera to be moving — they function independently of camera behavior.

**Verified:** Describing the temporal structure of a clip (what happens in the first 4 seconds vs. the second 4 seconds) influences the pacing and development of the motion within the clip.

**[Requires verification]:** The reliability of the 0–4s/4–8s temporal split instruction across different scene types. In complex scenes with many elements, the model may not fully honor the phase structure.

### 2.4 Lighting Interpretation

**Verified:** Veo interprets physical light source descriptions — source type, direction, temperature character, and falloff behavior — and produces corresponding lighting output. The more physically specific the description, the more consistent the output.

**Verified vocabulary that Veo responds to:**

- Source character: `warm amber`, `copper-amber`, `torch-warm`, `harsh noon glare`, `overcast diffuse`, `warm gaslight`, `oil-lamp warmth`, `stone-grey reflected`
- Entry direction: `from the upper left`, `from the right`, `descends from overhead`, `rakes across from the right`
- Entry verbs: `enters`, `descends`, `arrives`, `rakes across`, `falls across`, `radiates outward`
- Falloff: `hard falloff to pure unlit black`, `rapid falloff to deep shadow`, `soft receding to warm dimness`, `no ambient fill`

**Verified:** Lighting described in terms of mood rather than physical source produces less consistent and less controllable output. "Dramatic warm lighting" is mood language. "Warm amber light raking across the stone surface from the upper left, hard falloff to pure unlit black" is source language.

**Verified:** Describing what falls into darkness — not just that darkness exists — produces more controlled tonal behavior. "The opposing face recedes into deep shadow with no ambient fill" is more effective than "dark shadows."

**[Requires verification]:** Whether specific Kelvin temperature values (2700K, 3200K) in the prompt text influence the actual color temperature of generated lighting, or whether character-based vocabulary (warm amber, stone-grey) is the effective mechanism.

### 2.5 Material Interpretation

**Verified:** Veo produces more accurate material surfaces when the material is described with its specific physical behavior under the scene's lighting conditions — not just its name or general type.

**Effective pattern:** `[material name] — [surface condition], [aging or wear state], [behavior under the scene's specific light]`

**Verified vocabulary:**
- Surface condition: `salt-bleached`, `water-darkened`, `mineral-stained`, `dust-covered`, `fire-blackened`, `worn smooth by foot traffic`
- Aging state: `oxidised surface`, `crumbling at exposed faces`, `checking along the grain`, `calcium carbonate stained`
- Light behavior: `warm amber catch on the raised faces`, `cool shadow in the recessed joints`, `catching raking light at the surface topography`, `diffuse reflection across the dampened face`

**Verified:** Generic material descriptions ("stone surface," "metal object," "old wood") consistently produce generic, studio-quality output that lacks period specificity. Specific material descriptions produce forensic surface detail.

### 2.6 Atmosphere Interpretation

**Verified:** Veo interprets specific named physical atmospheric phenomena and produces them in the output. The atmospheric element must be physically described — named phenomenon, behavior, location within the frame.

**Verified atmospheric vocabulary:**

- `torch smoke drifting upward toward the vault ceiling` — produces upward-drifting smoke
- `dust particles suspended in the key light beam, tracing its angle` — produces visible particulate in light
- `moisture condensation on the stone face, beaded across the surface` — produces surface moisture
- `heat shimmer over sun-heated stone` — produces heat distortion
- `airborne stone dust from recent excavation disturbing the near-field light` — produces near-field particle scattering

**Verified:** Generic atmospheric descriptions ("mysterious atmosphere," "foggy and atmospheric") produce generic visual outputs — typically uniform mist or undefined haze. Specific physical phenomena descriptions produce specific physical behaviors.

**Verified:** Atmospheric elements described as physically present in the frame (not just in the scene as a whole) are rendered more reliably. Placing the atmospheric element in relation to the light source anchors it in the frame.

### 2.7 Audio Interpretation

**Verified:** Veo 3 generates audio as part of its output by default. The generated audio reflects the inferred physical content of the scene — crowds produce crowd noise, water produces water sounds, fire produces crackling, mechanisms produce mechanical sounds, ceremonial spaces produce chanting or scored atmosphere.

**Verified:** Audio generation in Veo 3 is driven by the visual content description. The audio is not a separate parameter that can be adjusted independently of the prompt — it is inferred from the scene.

**[Requires verification]:** The specific mechanism for declaring intended audio behavior in Veo prompts, and whether that declaration reliably suppresses inferred audio. The Video Style System (§21.3) requires that every generation request include an explicit audio intent declaration. The exact prompt phrasing that produces the most reliable audio suppression has not been confirmed through systematic testing. The CPA must establish the current best-known approach and flag it for verification when results are observed.

**[Requires verification]:** Whether Veo supports an API-level audio parameter (separate from prompt text) that controls audio generation behavior, or whether audio control is exclusively through prompt language.

### 2.8 Subject Composition

**Verified:** Veo has a bias toward centering the primary subject in the frame. Without explicit compositional instruction, the primary subject tends to occupy the center of the frame, particularly for close-up and medium shots.

**Verified:** Explicit position instructions override this default. Rule-of-thirds language ("primary subject in the right 40% of frame," "subject positioned at left-of-center") is interpreted and acted on.

**Verified:** Wide establishing shots are less subject-centric — the model distributes visual weight across the environment when the shot scale implies environment-primary composition.

### 2.9 Negative Constraint Interpretation

**Verified:** Veo responds to bracketed negative constraints formatted as `[no X]` or `[without X]`. These suppress the model's default failure modes for specific scene types.

**Verified:** `[no vignette]` suppresses the default post-processing vignette effect that Veo applies to many outputs. This is one of the most consistently honored negative constraints.

**Verified:** Anachronism-specific negative constraints (`[without modern watercraft]`, `[no fluorescent lighting]`, `[without contemporary signage]`) help suppress period violations that the model would otherwise introduce based on visual similarity to modern reference material.

**[Requires verification]:** Whether all negative constraints carry equal weight, or whether some categories (format/technical constraints like `[no vignette]`) are more reliably honored than aesthetic/period constraints (`[without modern industrial elements]`).

**[Requires verification]:** Whether there is an effective maximum number of negative constraints beyond which additional constraints are ignored or produce diminishing returns.

### 2.10 Technical Tag Interpretation

**Verified:** Veo responds to quality and format tags in bracketed format:

- `[4K]` — influences output quality character (higher detail, less generation noise)
- `[24fps]` — influences the motion feel of the clip (cinematic frame rate character)
- `[aspect ratio: 16:9]` / `[aspect ratio: 9:16]` — controls the output aspect ratio
- `[lamp source off-frame]` — suppresses the model's tendency to render the physical light fixture in frame when a practical source is named in the prompt

**[Requires verification]:** Whether `[4K]` affects the actual render resolution or only the quality character of the output within the provider's generation pipeline.

---

## 3. Translation Rules

This section maps each canonical concept from the Video Style System to its corresponding Veo prompt expression. The canonical concept is the rule. The Veo expression is how to communicate that rule in language Veo acts on.

### 3.1 Color System (§7)

**Canonical:** The channel identity palette defines specific hex ranges for shadow, mid tones, key light, and cool accent.

**Veo behavior:** Veo does not interpret hex color values as generation parameters. Color in the output is produced by describing the character and behavior of light sources and the materials they illuminate. The correct color emerges from the correct light and material descriptions — it cannot be specified directly.

| Canonical Color Concept | Veo Prompt Expression |
|---|---|
| Deep void shadow (#080808–#141414) | `hard falloff to pure unlit black` / `no ambient fill` / `shadow recedes to absolute darkness` |
| Warm dark mid (#1F1A14–#2E2519) | `warm amber edge where the key light falls off, resolving to dark brown-black at depth` |
| Bronze/aged metal (#836333) | `bronze surface with oxidised brown-green patina on aged faces, warmer copper at wear points` |
| Amber/gold key light (#C4A642–#D4AF37) | `warm amber light`, `copper-amber torch warmth`, `warm golden raking light` |
| Cool atmospheric accent (#4A6080–#6A8CB0) | `stone-grey reflected ambient from the opposing surface`, `cool overcast diffuse from the opening above`, `warm silver moonlight` |
| Warm zones 35–50% of frame | Achieved through falloff language: key light illuminates 35–45% of frame area; remainder falls to shadow |
| Cool accent maximum 15–20% | Describe cool source as secondary and subordinate: `a cooler stone-grey ambient enters from above, providing edge separation only` |

### 3.2 Lighting System (§8)

**Canonical:** Every scene requires a warm key light (motivated, directional, period-plausible source) and a secondary cooler source (edge separation only, never competing for dominance).

**Veo behavior:** Lighting must be physically described — source character, direction of entry, what it illuminates, and how it falls off. Mood-based lighting descriptions produce inconsistent and often flat output.

| Canonical Lighting Requirement | Veo Prompt Expression |
|---|---|
| Warm key light, period-plausible source | `warm amber light enters from the upper left, raking across the stone face` — name the physical behavior, not the fixture |
| Key light direction 30–60° lateral, 20–40° elevated | `from the upper left` / `from the right at a 45° angle` / `descends at an oblique angle from above-right` |
| Sharp falloff, illuminated zone ≤ 35–45% of frame | `hard falloff to pure unlit black` / `rapid falloff to deep shadow beyond the lit face` |
| Secondary cooler source, edge separation only | `a cooler stone-grey ambient enters from above-right, separating the subject's edge against the dark background; it does not compete with the key light on the primary surface` |
| Flat frontal lighting forbidden | Negative constraint: `[no flat fill]` / `[no frontal fill]` / absence of any instruction suggesting even illumination |
| Uniform ambient forbidden | `[no ambient fill]` / `[no even illumination]` — must be present in Layer 9 for interior scenes |
| Lamp source off-frame | Tag: `[lamp source off-frame]` — required whenever a practical period source is named. Do not describe the fixture itself. |

### 3.3 Contrast and Shadow (§9)

**Canonical:** High directional contrast. True near-black shadows. Warm amber highlights, not neutral white. Contrast ratio 8:1 to 12:1.

**Veo behavior:** Contrast is controlled through falloff language and shadow description. Describing what falls into darkness — and naming the darkness explicitly — produces truer blacks. Neutral white highlights are avoided by anchoring specular highlights to warm material character.

| Canonical Contrast Requirement | Veo Prompt Expression |
|---|---|
| True near-black shadows, not lifted grey | `hard falloff to pure unlit black` / `shadow recedes without grey lift` / `[no ambient fill]` |
| Warm amber highlights, not neutral white | `warm amber catch light at the specular peak` / `highlight resolves to copper-gold at the brightest point` / `[no neutral white highlights]` |
| Shadow edges semi-hard to hard | `semi-hard shadow edges tracing the surface topography` / `hard-edged shadow at the key light boundary` |
| Key-lit zone ≤ 35–45% of frame | Control through falloff language + negative space description + subject placement |

### 3.4 Atmosphere (§10)

**Canonical:** Every scene requires atmospheric evidence of physical inhabitation. Underground spaces require particulate, combustion haze, or moisture. Exterior spaces require dust, haze, or environmental particulate.

**Veo behavior:** Atmospheric elements must be named as specific physical phenomena with their location in the frame and their relationship to the light source. Generic atmosphere terms produce generic output.

| Canonical Atmospheric Type | Veo Prompt Expression |
|---|---|
| Underground suspended particulate | `stone dust suspended in the key light beam, tracing its angle down from the upper left` |
| Combustion haze from torchlight | `torch smoke drifting upward, accumulating against the vault ceiling above the lit zone` |
| Moisture on stone | `moisture condensation beaded across the lower stone face, catching diffuse ambient as fine reflective points` |
| Exterior dust | `airborne excavation dust disturbing the near-field light, diffusing the hard shadow edges` |
| Atmospheric depth haze | `heat shimmer rising from the sun-heated stone surface at depth, softening the background plane` |
| Sea spray at distance | `sea-salt haze diffusing the horizon line, the distant cliff face softening to grey-white` |

### 3.5 Camera Language (§11)

**Canonical:** Every camera movement must serve a specific narrative function. One movement type per scene. No unmotivated drift. No dutch tilt. Handheld restricted to POV exploration. Worm's-eye restricted to ceiling-vault-as-subject.

**Veo behavior:** Single motivated camera verb + speed qualifier is the reliable pattern. Two concurrent movement types produce inconsistent output.

| Canonical Narrative Function | Veo Prompt Expression |
|---|---|
| Discovering a subject | `controlled slow push-in` |
| Withdrawing from subject | `controlled pull-back` |
| Surveying a surface | `slow pan` / `lateral tracking shot` |
| Descending into space | `slow vertical descent` / `overhead descent` |
| Ascending | `slow vertical ascent` |
| Revealing architectural scale | `controlled pull-back to wider frame` |
| Revealing cross-section | `slow tilt along the section axis` |
| Following structure through depth | `controlled slow push-in along the tunnel axis` |
| Encircling for three-dimensionality | `slow orbital arc around the subject` |
| Establishing (permitted hold) | `controlled wide establishing hold` — explicitly permitted for establishing scenes |
| POV exploration (only handheld use) | `handheld drift forward through the space` — explicitly name POV function |

**Speed qualifiers:** `controlled slow` (most deliberate), `deliberate`, `measured`, `gentle`. Never omit the speed qualifier.

### 3.6 Lens Language and Shot Scale (§12)

**Canonical:** Nine permitted shot scales, each with a specific narrative function. Wide to medium depth of field as default. No extreme shallow focus as default beautification.

**Veo behavior:** Shot scale vocabulary is well-processed by Veo. Use the exact terms from the permitted list — Veo's training includes standard cinematography vocabulary.

| Canonical Shot Scale | Veo Prompt Term | Notes |
|---|---|---|
| Extreme close-up | `extreme close-up` | Material evidence at full legibility |
| Close-up | `close-up` | Subject at full identification |
| Medium close-up | `medium close-up` | Subject with partial environmental context |
| Medium | `medium shot` | Subject and immediate environment, equal weight |
| Medium wide | `medium wide shot` | Subject within larger environment |
| Wide establishing | `wide establishing shot` | Environment as primary |
| Overhead | `overhead shot` / `top-down view` | Plan view only |
| Macro | `macro shot` | Extreme surface magnification |
| Low angle | `low angle shot` | Scale revelation from below |

**Depth of field:** State explicitly when functional shallow focus is used: `[depth of field: shallow — foreground subject on one focal plane, background falls to soft]`. State wide as default for establishing: `[depth of field: wide — full environment visible from foreground to background]`.

### 3.7 Composition and Spatial Layering (§13)

**Canonical:** Primary subject in right 40–60% of frame. Every frame must have minimum two spatial layers. One primary subject per frame. Near-black maximum 65% of frame area.

**Veo behavior:** Compositional position language is interpreted reliably. Spatial layering must be described as physical elements at different distances — not implied.

| Canonical Composition Requirement | Veo Prompt Expression |
|---|---|
| Primary subject right 40–60% | `subject positioned in the right half of the frame` / `subject at right-of-center, occupying the right 40%` |
| Centered subject (architectural symmetry) | `subject centered in the frame on the architectural axis` |
| Left-dominant (leading eye into depth) | `subject at left-of-center, leading the eye toward depth at frame right` |
| Minimum two spatial layers | Describe both: `[foreground element] in the near field; [background element] receding at depth` |
| Three preferred layers | `[foreground element] at near field; [subject] at midground; [background recession] at depth` |
| Foreground element vocabulary | `the near edge of the arch entering frame-left`, `a vault rib in the near field`, `atmospheric haze at close distance`, `a period object at close range` |

### 3.8 Subject Scale (§14)

**Canonical:** Two valid approaches — monumental (subject 45–65% of frame height) or scale contrast (subject 5–15% of frame height). Forbidden zone: 20–40% of frame height.

**Veo behavior:** Subject scale in the frame is controlled through shot scale selection (§3.6) combined with explicit scale language in the subject declaration and composition description.

| Canonical Scale Approach | Veo Prompt Expression |
|---|---|
| Monumental (45–65% height) | Close-up or medium close-up + `subject fills the upper frame, occupying the dominant vertical proportion` |
| Scale contrast (5–15% height) | Wide establishing + `a human figure at 8% of frame height in the lower third` / `a small figure dwarfed by the vault height` |
| Scale anchor required | Name it explicitly: `a torch bracket of recognisable scale at frame-left`, `a doorway at human scale establishing the corridor dimensions`, `a single figure silhouetted at distance` |
| Forbidden zone (20–40%) — avoid | Do not describe a subject as partially filling the frame without strong environmental context. Shot scale must commit to one of the two valid approaches. |

### 3.9 Environmental Storytelling (§15)

**Canonical:** Every subject must exist within a plausible ancient physical environment. No void backgrounds. Environment must provide spatial information.

**Veo behavior:** Environmental context must be described as physical architecture or landscape elements present in the frame — not implied by the subject's identity. Without explicit environmental description, Veo defaults to a void or neutral background, particularly for close-up and medium shots.

| Canonical Environmental Requirement | Veo Prompt Expression |
|---|---|
| Non-void environment required | Name at least one environmental element in background description: `[architectural feature] recedes at depth` |
| Spatial evidence: where | `Roman brick-vaulted corridor extending into depth at the right`, `the hypogeum floor plan visible across the chamber floor` |
| Spatial evidence: what was done here | `worn stone at the threshold from centuries of foot traffic`, `soot darkening the vault above the torch bracket position` |
| Spatial evidence: use history | `water channel cut into the floor surface`, `rope grooves worn into the edge of the loading shaft` |

### 3.10 Material Authenticity (§16)

**Canonical:** Every scene must contain at least one material observation specific to the period, environment, and lighting conditions. Materials must show age and use history.

**Veo behavior:** Material description following the pattern `[material name] — [surface condition], [aging indicator], [behavior under scene's specific light]` produces the highest fidelity surface output.

| Period Material | Veo Prompt Expression Pattern |
|---|---|
| Travertine | `travertine surface — dense cream-to-ochre face, raking torch light catching the raised surface topography in warm amber, cool shadow in the recessed joint lines` |
| Tuff (tufa) | `volcanic tufa — dark grey-green, soft and rough-cut, moisture-darkened at the base from groundwater contact` |
| Roman concrete | `opus incertum wall — irregular tuff aggregate in thick white lime mortar, mortar joints wider and more varied than modern pointing, warm amber catch on protruding aggregate faces` |
| Fired brick | `terracotta brick — slightly variable red-orange across a single course, thin lime mortar lines, warm amber at the exposed brick face in raking light` |
| Timber | `ancient timber — axe-cut grain, checking along the growth rings, warm amber-brown deepened by age and moisture, no saw-cut precision` |
| Hemp rope | `hemp rope — warm beige-brown, individual fibres visible in the key light, fraying at the termination point, slightly darkened by moisture contact` |
| Age indicators | `mineral staining`, `iron ochre runs`, `soot blackening`, `lichen on exterior faces`, `root penetration at mortar joints`, `subsidence crack at the vault key` |

### 3.11 Historical Authenticity — Anachronism Suppression (§17)

**Canonical:** No modern light sources, no modern construction materials, no contemporary human elements, no modern infrastructure visible.

**Veo behavior:** Anachronism suppression is most effective through explicit negative constraints naming the specific modern elements most likely to appear for the scene type.

| Scene Type | Recommended Negative Constraints |
|---|---|
| Underground Roman interior | `[without fluorescent lighting]` `[without electric light fixtures]` `[without modern drainage infrastructure]` `[without steel reinforcement]` `[without contemporary safety signage]` |
| Archaeological excavation exterior | `[without modern excavation machinery]` `[without contemporary protective fencing]` `[without synthetic materials]` `[without modern footwear]` |
| Mechanical artifact close-up | `[without modern display case]` `[without museum label or scale reference]` `[without institutional lighting]` `[without synthetic display surface]` |
| Mediterranean exterior | `[without modern watercraft]` `[without contemporary coastline infrastructure]` `[without modern clothing]` |

**Face prohibition (§17.3):** Negative constraints for frontal faces: `[no frontal faces]` `[figures from behind or in silhouette only]` `[no portrait framing]`.

### 3.12 Realism — CGI and Illustration Suppression (§18)

**Canonical:** No CGI appearance, no illustration or concept-art register, no game-engine appearance.

**Veo behavior:** CGI-appearance suppression works through both positive material specificity (see §3.10) and explicit negative constraints. The positive approach (describing physically real material behavior) is the more effective primary mechanism; negative constraints reinforce it.

| CGI Failure Mode | Negative Constraint |
|---|---|
| Uniformly smooth surfaces | `[no perfectly smooth surfaces]` `[no uniform digital textures]` |
| Even illumination | `[no ambient fill]` `[no even lighting]` `[no softbox lighting]` |
| Concept-art palette | `[no stylised color grading]` `[no painterly rendering]` |
| Game-engine surface normals | Addressed through material specificity — describe physical surface variation in Layer 4 |
| Artificial bloom | `[no lens flare]` `[no artificial bloom]` |

### 3.13 Motion and Temporal Structure (§19)

**Canonical:** Every 8-second clip must contain a physical change between 0–4s and 4–8s. Every clip must contain at least one secondary motion element. Camera energy may intensify across the episode arc.

**Veo behavior:** The 0–4s/4–8s temporal structure is honored more reliably when the phase descriptions are physically specific rather than emotional or narrative.

| Canonical Requirement | Veo Prompt Expression |
|---|---|
| Physical change at 4s | `0-4s: [specific physical state established]. 4-8s: [specific physical change that differs from the opening state].` |
| Secondary motion element | Name it in the motion qualifier after the camera verb: `dust particles drift through the key light beam throughout the clip`, `torch smoke rises continuously into the vault above the lit zone` |
| Phase 1 establishes | Describe the spatial and material condition the viewer enters: what is visible, where the camera is, what state the subject is in |
| Phase 2 develops | Describe the specific change: camera has arrived at its destination, light condition has shifted, a physical state has changed, depth layer has been revealed |

**Pattern:** `Camera: [verb + speed]. Subject: [specific physical behavior]. [Named secondary motion element].`

### 3.14 Audio Policy (§21)

**Canonical:** Generated clips must contain only natural environmental ambience. No music. No speech or intelligible voice. No narration. No modern audio signatures. Audio intent must be explicitly declared — not assumed.

**Veo behavior:** [See §2.7 — the specific prompt phrasing that produces reliable audio suppression requires verification.] The current best-known approach is to declare audio intent explicitly in the prompt as both a positive specification (what is permitted) and a prohibition (what is excluded). This declaration should be included in Layer 8 Technical Tags or as a concluding constraint in Layer 9.

**Current approach (pending verification):** Describe permitted audio content explicitly: `natural stone acoustics, distant environmental sound consistent with the physical space, no music, no speech, no narration`. The specific tag format that Veo honors most reliably should be documented from production observations.

| Canonical Audio Prohibition | Current Prompt Approach (requires verification) |
|---|---|
| No music | Include music prohibition in audio declaration |
| No speech / narration / intelligible voice | Include voice prohibition in audio declaration |
| No generated SFX designed for mixing | Implicit — covered by positive declaration of ambient-only intent |
| No modern audio signatures | Implicit — covered by physical environment description |

**Priority:** The audio policy (§21) is ranked second in Rule Priority (§29) — above coverage and visual hierarchy rules. This ranking must be reflected in the CPA: the audio intent declaration is not optional and is not deprioritised when the prompt is simplified.

### 3.15 Scene Narrative Focus (§33)

**Canonical:** One primary narrative focus per scene. One primary visual action per scene. Secondary environmental motion permitted if it supports the primary focus.

**Veo behavior:** Veo produces more coherent output when the subject declaration names one primary subject and one primary action. When multiple subjects and multiple simultaneous actions are described, the output tends toward compositional chaos — multiple subjects competing without clear hierarchy.

| Canonical Focus Rule | Veo Prompt Expression |
|---|---|
| One primary subject | Name the primary subject once, clearly, in the subject declaration (Layer 2). All other elements are environmental context. |
| One primary visual action | State one physical event the viewer tracks: `the mechanism turns slowly as the light rakes across its surface`, not `the mechanism turns while smoke rises while water drips while the torch flickers` |
| Secondary environmental motion supports | Secondary elements named in Layer 6 motion qualifier, not in Layer 2 subject declaration |
| Support test in practice | If removing the secondary element from the prompt makes the primary subject clearer — it belongs in Layer 9 as a constraint, not in Layer 6 as a motion element |

---

## 4. Prompt Construction Best Practices

These practices reflect observed Veo behavior and the CPA's existing layer structure. They are not style rules — they are translation efficiency principles.

### 4.1 Subject First

Place the primary subject declaration early in the prompt (Layer 2 position). Subject declarations that appear late in the prompt, after extensive atmospheric or environmental description, produce weaker subject behavior in the output — the model has already committed visual weight to the early-declared elements.

**Correct order:** Shot scale → primary subject → lighting → material → environment → camera → motion → tags → constraints.

**Do not:** Open with atmosphere or mood. Do not open with the environment before the subject has been declared.

### 4.2 Physical Specificity Over Abstraction

At every layer, replace abstract or mood-based language with physical observation language.

| Abstraction | Physical Equivalent |
|---|---|
| `mysterious atmosphere` | `torch smoke accumulated at the vault ceiling, tracing the warm light beam below` |
| `dramatic lighting` | `warm amber light raking from the upper left, hard falloff to unlit black at the right` |
| `ancient stone` | `travertine — dense cream surface, raking torch light catching raised topography in amber` |
| `the camera moves slowly` | `controlled slow push-in` |
| `an old wooden beam` | `ancient timber — axe-cut grain with checking, warm amber-brown, moisture-darkened at the joints` |

### 4.3 Ambiguity Avoidance

Veo cannot resolve ambiguous or contradictory instructions — it produces generic output when given conflicting direction. The prompt must commit to one direction for every dimension:

- One warm/cool balance (not "warm but with a cool cast")
- One camera movement type (not "push in while drifting laterally")
- One compositional energy (not "dramatic but restrained")
- One atmospheric register (not "underground but with bright open air feel")

When the canonical intent requires nuance ("warm dominant with a cooler secondary source"), express the nuance as two distinct, non-competing elements: primary warm key from one direction, secondary cool from a different direction that never competes with the key on the primary subject.

### 4.4 Environment Description Order

Describe spatial layers in order from background → midground → foreground. This mirrors natural visual reading order (eye starts at depth and returns to near field) and helps Veo construct believable spatial depth.

**Background first:** What the deep background plane contains and how it recedes. Then midground (the subject zone). Then foreground (the near-field element that establishes the camera's physical position inside the space).

### 4.5 Negative Constraint Placement and Scope

Place negative constraints in Layer 9 (last). Negative constraints earlier in the prompt consume weight that should go to positive description.

Write the most scene-specific constraints first within Layer 9. Generic constraints (`[no vignette]`) go last because they apply universally and carry less contextual weight.

**Scope negatives correctly:** Name the specific element to suppress, not a general category. `[without modern drainage pipes]` is more specific than `[nothing modern]`. Specific constraints are more reliably honored than broad category exclusions.

### 4.6 Falloff Language Is Not Optional

Every lighting description must include falloff language. Without falloff, Veo defaults toward diffuse ambient illumination — the opposite of the channel's visual character. Falloff language is the mechanism that produces the channel's defining high-contrast tonal profile.

**Required pattern:** `[light enters from direction], [illuminating named feature]; [falloff language naming what recedes into darkness].`

### 4.7 Temporal Structure Is a Physical Contract

The 0–4s/4–8s temporal description is a physical contract between the prompt and the clip output. Both phases must describe observable physical states — not emotional arcs, not narrative intentions, not editorial purposes.

**Valid:** `0-4s: the fragment emerges from shadow as light resolves across the corroded face. 4-8s: light temperature deepens to copper-orange, shadow edges harden, void consolidates behind the fragment.`

**Invalid:** `0-4s: sense of discovery builds. 4-8s: the viewer understands the significance of the object.`

---

## 5. Prompt Anti-Patterns

This section documents failure patterns that consistently produce poor Veo output. Each entry names the anti-pattern, its typical symptom, and the correct approach.

### AP-01 — Stacked Camera Movements

**Pattern:** Two camera movement types in one prompt. `dolly forward while panning right`, `orbit with a simultaneous tilt`, `tracking while drifting laterally`.

**Symptom:** Veo picks one movement and ignores or weakly honors the other. Output is inconsistent across generations — sometimes one movement, sometimes the other, sometimes neither clearly.

**Fix:** One movement verb per prompt, derived from the scene's single narrative function (§11.2). If two movements seem required, the scene requires two clips.

### AP-02 — Contradictory Adjectives

**Pattern:** Opposing descriptors for the same quality. `dynamic but subtle`, `energetic but calm`, `warm but with a cool feel`, `minimal yet rich in detail`.

**Symptom:** Veo cannot reconcile the contradiction and defaults to a generic middle — output that satisfies neither direction and reads as unmotivated.

**Fix:** Commit to one direction. If warmth with some coolness is required, describe both as distinct physical elements at specific positions in the frame — not as contradictory properties of the same element.

### AP-03 — Mood Vocabulary Without Physical Anchor

**Pattern:** Describing a scene's atmosphere through emotional or narrative language without physical specificity. `mysterious and ancient`, `dark and foreboding`, `dramatic and powerful`, `hauntingly beautiful`.

**Symptom:** Veo produces visually generic output — often clichéd compositions, stock-footage aesthetics, generic moody lighting with no directional character. The emotional label has no physical production instruction attached to it.

**Fix:** Replace every mood adjective with a physical observation that would produce that mood as a result. `mysterious` → `torch smoke accumulated at the vault ceiling, light failing before it reaches the far wall`. The mood emerges from the physical description; it is not described as a target.

### AP-04 — Generic Material Description

**Pattern:** Naming the material without describing its surface behavior under the scene's light. `stone`, `metal`, `wood`, `ancient ruins`, `old brickwork`.

**Symptom:** Veo produces generic, studio-quality surface rendering — smooth, uniformly lit, lacking the material specificity that produces forensic surface detail and period credibility.

**Fix:** Every named material must include its surface condition, aging state, and behavior under the scene's specific lighting (§3.10). Generic nouns are not sufficient.

### AP-05 — Overcrowded Subject Declaration

**Pattern:** Multiple subjects, multiple simultaneous actions, multiple elements competing for primary focus. `a Roman soldier inspects artifacts while slaves work the mechanism and torches flicker and guards watch from above`.

**Symptom:** Veo distributes visual weight across all described elements, producing a chaotic composition with no clear primary subject. The output reads as a crowded scene, not as documentary evidence.

**Fix:** One primary subject, one primary visual action. Secondary environmental elements support the primary in Layer 6 motion qualifier — they are not equal subjects named in the Layer 2 declaration.

### AP-06 — Camera Overload

**Pattern:** Describing camera behavior with more qualifiers than Veo can resolve. `controlled, slow, deliberate, measured push-in with a subtle drift and a slight pan as it advances and a very gentle upward tilt`.

**Symptom:** Veo cannot execute compound camera instructions reliably. Output tends toward a generic floating motion that satisfies none of the described qualities specifically.

**Fix:** One camera verb, one speed qualifier. `controlled slow push-in`. If subtle drift is needed, it is a secondary motion element, not a camera verb.

### AP-07 — Lighting Without a Source

**Pattern:** Describing the quality of light without a physical source or direction. `warm, atmospheric lighting fills the space`, `dramatic shadows play across the surface`, `beautiful golden light bathes the scene`.

**Symptom:** Veo produces diffuse, directionless warmth — the opposite of the channel's sharp, motivated lighting character. Without a source and direction, light has no shadow.

**Fix:** Every lighting description names a physical source character, a direction of entry, what it illuminates, and how it falls off (§3.2).

### AP-08 — Style Mixing

**Pattern:** Combining vocabulary from incompatible aesthetic registers within one prompt. `photorealistic archaeological documentary` alongside `cinematic concept art style` or `beautiful rendering` or `3D animation quality`.

**Symptom:** Veo cannot produce a consistent aesthetic when given mixed register instructions. Output frequently drifts toward the more specific of the two registers — often the CGI register, since it has more distinct vocabulary in training data.

**Fix:** Use only documentary-register vocabulary throughout. Never describe the desired output as a style of media — describe only what the camera observes as physical reality.

### AP-09 — Front-Loaded Negative Constraints

**Pattern:** Beginning a prompt with what is not wanted. `No neon lights, no modern elements, no digital effects, no CGI, no talking heads...` before describing what is wanted.

**Symptom:** Veo interprets the front-loaded negatives as signals about what the scene contains, occasionally producing the negated elements despite the prohibition. The most effective placement for negatives is at the end (Layer 9).

**Fix:** Describe what the scene IS before describing what it is NOT. Negatives belong in Layer 9, after all positive description is complete.

### AP-10 — Atmosphere Without Physical Location

**Pattern:** Describing atmospheric conditions without placing them in the frame. `thick smoke fills the air`, `haze everywhere`, `dusty atmosphere throughout`.

**Symptom:** Uniform atmospheric coverage without spatial direction — the atmosphere reads as a filter applied to the image, not as a physical condition present in a specific zone of the frame.

**Fix:** Place the atmospheric element relative to the light source and a specific spatial zone. `torch smoke drifting upward and accumulating at the vault ceiling above the lit zone, visible only where the key light beam passes through it at mid-height`.

---

## 6. Provider Mapping

This table maps each major canonical rule (§ref) to its prompt requirement: whether the rule must be explicitly communicated in the prompt, can be partially implied through related language, or applies only to generation parameters outside the prompt.

| §Ref | Rule | Prompt Requirement | Explicit Expression Required |
|---|---|---|---|
| §7.1 Color palette | Channel identity hex values | IMPLICIT | Colors emerge from light + material descriptions. No hex values in prompt. |
| §7.2 Warm/cool balance | 35–50% warm, 50–65% dark, max 15–20% cool | SEMI-EXPLICIT | Controlled through falloff language and secondary source description. |
| §7.4 Forbidden colors | No neon, electric blue, green-dominant, etc. | EXPLICIT (Layer 9) | Name the specific forbidden colors as negative constraints for the scene type. |
| §8.1 Two light sources | Warm key + cooler secondary | EXPLICIT (Layer 3) | Both sources must be named with character, direction, and position. |
| §8.2 Key light character | Warm, directional, period-motivated, sharp falloff | EXPLICIT (Layer 3) | Must name source character, direction, and falloff explicitly. |
| §8.3 Secondary source | Cooler, edge separation, not competing | EXPLICIT (Layer 3) | Must name the secondary source and its subordinate function. |
| §8.4 Forbidden lighting | No flat fill, ring, softbox, fluorescent, ambient | EXPLICIT (Layer 9) | `[no ambient fill]` `[no flat fill]` always present for interiors. |
| §9.1 Shadow blacks | True near-black, not lifted | EXPLICIT (Layer 3) | Falloff language: `hard falloff to pure unlit black`. |
| §10.1 Atmospheric evidence | Required in every interior + exterior scene | EXPLICIT (Layer 4/6) | Named physical atmospheric phenomenon with its frame location. |
| §11.1 Camera height | Standard, low, high, overhead — restricted uses | EXPLICIT (Layer 2) | Shot scale selection implies height; low/overhead must be explicitly named. |
| §11.2 Motivated movement | One movement type per narrative function | EXPLICIT (Layer 6) | One camera verb + one speed qualifier per prompt. |
| §11.3 Forbidden behaviors | No unmotivated drift, no split paths, no dutch tilt, no decorative orbit | EXPLICIT (Layer 9) | Dutch tilt, split-screen, decorative orbit as negative constraints when scene type increases risk. |
| §11.4 Maximum hold | 16 seconds | PLANNING (not prompt) | Handled at scene planning stage — no prompt expression needed. |
| §12.1 Shot scale | Nine permitted types | EXPLICIT (Layer 2) | Named shot scale in subject declaration using canonical vocabulary. |
| §12.2 Depth of field | Wide to medium default, shallow functional only | EXPLICIT (Layer 8 tag) | `[depth of field: wide]` default; `[depth of field: shallow]` when functionally required. |
| §13.1 Subject placement | Right 40–60% default | EXPLICIT (Layer 5) | Named position in spatial composition layer. |
| §13.3 Spatial layers | Minimum two: midground + background | EXPLICIT (Layer 5) | Both layers named with physical elements. Foreground preferred but optional. |
| §14.1 Scale approach | Monumental or scale-contrast; no 20–40% zone | EXPLICIT (Layer 2 + 5) | Shot scale + subject size language controls the valid approach selection. |
| §14.2 Scale anchor | Required in every main episode scene | EXPLICIT (Layer 5) | Named scale reference element in spatial composition. |
| §15.1 Environmental context | No void backgrounds | EXPLICIT (Layer 4/5) | Background description must name a physical environmental element. |
| §16.1 Material specificity | One period-specific material observation per scene | EXPLICIT (Layer 4) | Named material + surface condition + aging + light behavior. |
| §16.2 Material age | Age and use history visible | EXPLICIT (Layer 4) | Age indicators named in Layer 4 observations. |
| §17.1 Anachronism prohibition | No modern light, materials, infrastructure | EXPLICIT (Layer 9) | Scene-type-specific negative constraints naming the most likely anachronisms. |
| §17.3 Face prohibition | No frontal faces | EXPLICIT (Layer 9) | `[no frontal faces]` `[figures silhouetted or from behind]` when human figures are present. |
| §18.1–18.3 CGI prohibition | No CGI, illustration, game-engine appearance | IMPLICIT + EXPLICIT | Primary defense: material specificity (positive). Secondary: targeted negative constraints. |
| §18.4 Photographic test | "Could a filmmaker have photographed this?" | IMPLICIT | Achieved through physical description language throughout. |
| §19.2 Temporal structure | Physical change between 0–4s and 4–8s | EXPLICIT (Layer 7) | Both phases described as physical states. |
| §19.3 Secondary motion | At least one ambient motion element | EXPLICIT (Layer 6) | Named in motion qualifier after camera verb. |
| §21.1 Permitted audio | Natural environmental ambience only | EXPLICIT (Layer 8/9) | **[Requires verification on exact form]** Declare permitted audio content explicitly. |
| §21.2 Prohibited audio | No music, speech, narration, modern SFX | EXPLICIT (Layer 8/9) | **[Requires verification on exact form]** Declare prohibitions explicitly. |
| §21.3 Audio intent declaration | Every clip must declare intended audio | EXPLICIT (Layer 8/9) | Audio intent is never assumed. Always present, never omitted. |
| §22.1 Provider-independent standards | Same visual standards regardless of provider | IMPLICIT | These are the cumulative result of all other explicit rules. |
| §23.1 8-second duration | All clips 8 seconds | GENERATION PARAMETER | Set in provider payload, not prompt text. |
| §27.1 Pre-generation validation | 12 checks before generation | VALIDATION GATE | Applied to the assembled prompt before submission. Not expressed within the prompt. |
| §33.1 One primary focus | One narrative claim per scene | IMPLICIT + EXPLICIT | Single subject in Layer 2. Single action in Layer 6. Secondary elements in their correct layers. |
| §33.2 One primary action | One foreground event | EXPLICIT (Layer 6) | Named in motion qualifier; secondary elements are supporting, not competing. |
| §34.1 Episode-level continuity | Consistent color temperature, atmosphere, lighting character across episode | TRANSLATION DISCIPLINE | Consistent vocabulary anchors used across all prompts in the same episode setting. The color temperature vocabulary, atmospheric description terms, and lighting character phrases must be identical across scenes sharing the same physical environment. |

---

## 7. Verified Examples

These miniature examples demonstrate translation quality — the difference between prompt language that produces canonical output and language that does not. These are not production prompts. They are narrow illustrations of single translation principles.

### Example 1 — Lighting: Poor vs. Canonical

**POOR (mood language, no source, no falloff):**
> *"The ancient Roman vault is lit dramatically with warm atmospheric lighting, casting mysterious shadows across the stone walls."*

**Why it fails:** No source direction, no falloff, no physical light entry. "Warm atmospheric" and "dramatic" are mood terms, not physical descriptions. Veo will produce diffuse ambient warm lighting — even, sourceless, lacking the directional drama required by §8.2.

**CANONICAL (physical source, direction, falloff):**
> *"Warm amber light enters from the upper left, raking across the travertine vault face at a shallow oblique angle; hard falloff to pure unlit black in the opposing shadow zone; no ambient fill in the upper vault."*

**Why it works:** Source character (warm amber), entry direction (upper left), physical behavior (raking), named illuminated feature (vault face), explicit falloff (hard to pure unlit black), explicit ambient prohibition.

---

### Example 2 — Camera: Poor vs. Canonical

**POOR (stacked movements, no speed qualifier):**
> *"The camera slowly orbits around the mechanism while gradually pushing in and panning slightly to keep it centered."*

**Why it fails:** Three concurrent movement instructions (orbit + push-in + pan). Veo cannot execute all three reliably. Output will be inconsistent, typically favoring one movement while ignoring the others.

**CANONICAL (one movement, one speed qualifier):**
> *"Camera: slow orbital arc around the mechanism, revealing its three-dimensional form progressively. Subject: mechanism remains stationary under the key light. Torch smoke drifts upward through the lit zone as the camera arcs."*

**Why it works:** One movement verb (orbital arc), one speed qualifier (slow), one narrative function (reveal three-dimensionality), one secondary motion element (torch smoke) — these do not compete.

---

### Example 3 — Material: Poor vs. Canonical

**POOR (generic noun, no surface behavior):**
> *"Old stone walls with some cracks and texture."*

**Why it fails:** "Old stone" has no period specificity. "Some cracks and texture" has no material character or lighting behavior. Veo will produce a generic digitally-rendered stone surface.

**CANONICAL (specific material, aging state, light behavior):**
> *"Opus incertum concrete — irregular tuff aggregate in thick white lime mortar, joints wider and more variable than modern pointing; warm amber catch on the protruding aggregate faces from the raking torch light; deeper mortar joints remain in cool shadow."*

**Why it works:** Named period material (opus incertum), described construction method (irregular aggregate), named aging characteristic (thick joints), specific light behavior (amber catch on protruding faces, cool shadow in joints).

---

### Example 4 — Atmosphere: Poor vs. Canonical

**POOR (filter description, no location, no physical behavior):**
> *"The underground chamber has a hazy, smoky atmosphere throughout, giving it a mysterious feel."*

**Why it fails:** "Hazy, smoky throughout" describes a uniform filter. "Mysterious feel" is mood language. Veo will produce uniform mist — the atmospheric void failure mode (§28.5).

**CANONICAL (specific phenomenon, location in frame, physical behavior):**
> *"Torch smoke drifts upward from below frame-left, accumulating against the vault ceiling above the lit zone; visible only where the key light beam passes through it at mid-height, tracing the beam's direction. The lower chamber air below the smoke layer is clear."*

**Why it works:** Specific phenomenon (torch smoke), named source location (below frame-left), physical behavior (drifts upward, accumulates at ceiling), relationship to light (visible only in the beam), cleared zone below (prevents uniform-mist failure mode).

---

### Example 5 — Audio Declaration: Current Approach (Pending Verification)

**POOR (no audio declaration):**
> *[Prompt describes a crowd of Roman workers operating the lifting mechanism in the hypogeum]*

**Why it fails:** Veo infers crowd noise, working sounds, and possibly chanting or score from the visual description. None of this is declared or controlled. The generated audio will compete with the narration track.

**CURRENT APPROACH (requires verification):**
> *At end of prompt: `natural stone acoustic ambience, structural settlement sounds, no crowd vocalisation, no music, no narration, no intelligible voice.`*

**Why this approach:** Explicitly declares permitted audio (stone acoustics) and prohibited audio (crowd vocalisation, music, narration). The exact form that most reliably suppresses audio must be confirmed through production observation. This example represents the current best-known approach, not a verified canonical form.

---

## 8. Future Extensions

This section reserves space for additional provider skills. Each provider has distinct prompt behavior. When a provider skill is created, it follows the same structure as this document: verified behavior only, no speculation, translation rules mapped to the same §refs in the Video Style System.

**No provider produces the same output from the same prompt.** The Video Style System rules remain constant. The translation expression differs per provider.

### Seedance Prompt Skill

`hypogeum/skills/seedance_prompt_skill.md`

**Scope when created:** Seedance-specific prompt behavior for camera movement, material rendering, temporal structure, and audio handling. Maps to the same §refs as this document.

**Status:** Not yet created.

### WAN Prompt Skill

`hypogeum/skills/wan_prompt_skill.md`

**Scope when created:** WAN-specific prompt behavior. Maps to the same §refs.

**Status:** Not yet created.

### Kling Prompt Skill

`hypogeum/skills/kling_prompt_skill.md`

**Scope when created:** Kling-specific prompt behavior. Maps to the same §refs.

**Status:** Not yet created.

### Runway Prompt Skill

`hypogeum/skills/runway_prompt_skill.md`

**Scope when created:** Runway-specific prompt behavior (Gen-4 and successors). Maps to the same §refs.

**Status:** Not yet created.

---

## Appendix A — Behaviors Requiring Verification

Summary of all items marked `[requires verification]` in this document. These must be confirmed through systematic production testing before the behaviors are treated as fixed architectural rules.

| # | Behavior | Section | Verification Method |
|---|---|---|---|
| V-01 | Exact prompt element weighting gradient (how steep the front-weighting decay is) | §2.1 | Controlled prompt-ordering tests with identical content in different positions |
| V-02 | Camera verb synonym equivalence (whether all synonyms for a movement type produce equivalent output) | §2.2 | Side-by-side generation comparison with synonym substitution |
| V-03 | Reliability of 0–4s/4–8s temporal split instruction across scene types | §2.3 | Frame-by-frame review of clips generated with explicit temporal structure |
| V-04 | Whether Kelvin values in prompt text influence actual output color temperature | §2.4 | Side-by-side comparison: character vocabulary vs. Kelvin value in identical prompts |
| V-05 | Specific prompt phrasing for audio intent declaration (most reliably honored form) | §2.7, §3.14 | Audio review of clips generated with different audio declaration phrasings |
| V-06 | Whether Veo supports an API-level audio parameter separate from prompt text | §2.7 | MUAPI and Veo API documentation review; provider capability testing |
| V-07 | Negative constraint weight equality (whether technical constraints are honored more reliably than aesthetic ones) | §2.9 | Generation review comparing `[no vignette]` vs. `[without modern infrastructure]` compliance rates |
| V-08 | Maximum effective number of negative constraints | §2.9 | Systematic constraint count testing: 4 vs. 6 vs. 8 constraints on comparable scene types |
| V-09 | Whether `[4K]` affects actual render resolution or only quality character | §2.10 | Resolution measurement of generated clips with and without the tag |

---

*This document is the permanent reference for Veo prompt translation. Every CPA version derives its vocabulary and structural decisions from the verified behaviors documented here. Behaviors marked [requires verification] must be promoted to verified status through production testing before they become hard CPA rules.*
