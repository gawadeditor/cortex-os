# Veo Translation Skill
## Translation Reference — Video Style System → Veo Generation Language

```
PROVIDER: Veo 3 / veo3-text-to-video (T1), veo3-fast-text-to-video (T2/T3), veo3-image-to-video (I2V) — MUAPI path
GENERATION_TYPE: T2V+I2V
SKILL_VERSION: 3.0
STYLE_SYSTEM_SYNCED: video_style_system.md v1.2
CREATED: 2026-07-07
LAST_UPDATED: 2026-07-08
HIERARCHY_POSITION: Video Style System → Veo Translation Skill → CPA → VBG → Runtime
PARENT_DOCUMENT: hypogeum/brand/video_style_system.md
DOWNSTREAM_DOCUMENT: hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md
```

---

## 1. Purpose

This document teaches the pipeline how Veo interprets prompt language. It does not define cinematic rules. It does not make creative decisions. Every cinematic decision belongs to `video_style_system.md`. This document exists solely to translate those canonical decisions into the vocabulary and structure that Veo reliably processes.

**What this document is:** A provider-specific translation reference for Veo. When the Video Style System specifies a cinematic requirement, this document explains how that requirement becomes prompt language that Veo acts on.

**What this document is not:**
- Not the canonical authority — `video_style_system.md` is the sole canonical source
- Not a runtime module — it executes nothing
- Not a validator — it checks nothing
- Not a prompt template — it produces no prompts
- Not the Cinematic Prompt Architecture — the CPA derives its layer structure from this skill

**The translation chain:**

```
Video Style System (what the output must be)
        ↓
Veo Translation Skill (how Veo processes language)
        ↓
Cinematic Prompt Architecture (the assembled prompt structure)
        ↓
Generated clip (the output)
```

**Traceability principle:** Every CPA convention applied to Veo-bound prompts must trace its vocabulary and structural choices to a specific behavior documented in this skill with a supporting Evidence ID and Level. If a CPA convention cannot be traced here, it is unverified for Veo.

---

## 2. Provider Profile

### 2a. Provider Identification

**Provider:** Veo 3 (Google DeepMind)

**MUAPI model identifiers (Hypogeum production pipeline):**
- `veo3-text-to-video` — T1 tier (premium/hero scenes)
- `veo3-fast-text-to-video` — T2/T3 tier (primary production and cheap-mode)
- `veo3-image-to-video` — I2V path

**API model identifiers (Google official):**
- `veo-3.1-generate-preview` — Veo 3.1 full model
- `veo-3.1-fast-generate-preview` — faster variant
- `veo-3.0-generate-001` — Veo 3 (audio confirmed on this model ID)

**Generation type:** T2V+I2V

### 2b. Supported Production Parameters

- **Durations:** 4, 6, or 8 seconds [Evidence: VEO-DOC-02, L1]
- **Aspect ratios:** 16:9 (1280×720), 9:16 (720×1280) [Evidence: VEO-DOC-03, L1]
- **Resolution:** 720p, 1080p, 4K (4K unavailable on Lite model) [Evidence: VEO-DOC-03, L1]
- **Audio:** default-on — generated natively alongside video [Evidence: VEO-DOC-01, L1]
- **I2V input format (MUAPI path):** `images_list` array [Evidence: VEO-HYP-01, L1 — configuration fact]
- **Text input limit:** 1,024 tokens [Evidence: VEO-DOC-04, L1]

### 2c. Known Quality Profile

- Official documentation lists a specific set of camera angles and camera movements as the supported vocabulary [Evidence: VEO-DOC-01, L2 — vocabulary documented; behavioral reliability is L5]
- Native audio generation: produces sound effects, ambient noise, and dialogue inferred from visual description by default [Evidence: VEO-DOC-01, L1]
- Supports reference-image input for style and character consistency via `ingredients-to-video` feature [Evidence: VEO-DOC-01, L1]
- Supports timestamp-based multi-shot sequencing within a single generation [Evidence: VEO-DOC-01, L2 — syntax documented]
- Dutch angle (canted horizon) is listed in official camera angle documentation [Evidence: VEO-DOC-01, L2] — it is prohibited by the channel's style system (§11.3), not by provider limitation
- Negative constraint behavior: official guidance recommends describing desired absence as nouns/descriptors rather than "no X" or "don't" phrasing [Evidence: VEO-DOC-01, L2 — documented recommendation]

### 2d. Behavior Summary

Veo is a high-fidelity cinematic video generation model with native audio output, official support for a broad range of camera movements and shot scales, and multi-modal input capability (text, reference images, I2V). Official documentation provides a structured prompt formula and specific camera vocabulary. The model's default behavior includes audio generation inferred from visual content, which must be addressed explicitly in every generation request (§21.3). Most fine-grained behavioral characteristics (how it responds to specific lighting descriptions, material specificity, atmospheric anchoring) are not formally documented and are currently classified as L5 (hypothesis) pending systematic validation.

---

## 3. Evidence Registry

| ID | Source Type | Name | URL | Version | Date Accessed | Max Claim Level |
|---|---|---|---|---|---|---|
| VEO-DOC-01 | OFFICIAL_DOC | Veo Prompt Guide (Gemini Enterprise Agent Platform) | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/video/video-gen-prompt-guide | Current | 2026-07-08 | L1, L2 |
| VEO-DOC-02 | OFFICIAL_DOC | Google Cloud Blog — Ultimate Prompting Guide for Veo 3.1 | https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-veo-3-1 | Published 2025-10-16 | 2026-07-08 | L1, L2 |
| VEO-DOC-03 | OFFICIAL_DOC | DeepMind Veo product page | https://deepmind.google/technologies/veo/ | Current | 2026-07-08 | L1 (marketing page — capability claims only, not syntax) |
| VEO-DOC-04 | OFFICIAL_DOC | Gemini API Veo documentation | https://ai.google.dev/gemini-api/docs/veo | Current | 2026-07-08 | L1, L2 |
| VEO-GEN-01 | COMMUNITY_SKILL | Square-Zero-Labs/video-prompting-skill (Veo3 model reference) | https://github.com/Square-Zero-Labs/video-prompting-skill | Current | 2026-07-08 | Section B use only; not evidence of Veo-specific behavior |
| VEO-HYP-01 | HYPOGEUM_EXP | Hypogeum MUAPI implementation (CLAUDE.md, config.py) | Internal | — | 2026-07-08 | L1 for configuration and format facts; L5 for behavioral claims |
| VEO-HYP-02 | HYPOGEUM_EXP | Hypogeum production observations (prior skill versions) | Internal | — | 2026-07-07 | L5 — no controlled methodology, no experiment IDs documented |

---

## 4. Verified Provider Behaviour (Section A)

**Scope:** Facts about what Veo does — API parameters, capability facts, officially documented behaviors and syntax. Sources qualifying for L1 or L2 only. Community skill claims appear here only when explicitly attributed to official documentation by the community skill.

### 4.1 API Parameters and Generation Defaults

**B-01 — Native Audio Generation**

Veo 3.0 and 3.1 generate audio as a native part of the video output. Audio is inferred from the visual content of the scene and includes sound effects, ambient noise, and where described, dialogue and background music.

- Evidence: VEO-DOC-01, VEO-DOC-03
- Level: L1
- §Ref: §21 Audio Policy

**B-02 — Audio Declaration Recommended**

Official documentation states: "Clearly specify if you want audio. We recommend that you use separate sentences in your prompt to describe the audio." This is documented guidance for prompt authoring.

- Evidence: VEO-DOC-01
- Level: L2 (documented recommendation — does not prove the declaration controls output)
- §Ref: §21.3 Audio Intent Declaration

**B-03 — Text Input Limit**

Maximum prompt length is 1,024 tokens.

- Evidence: VEO-DOC-04
- Level: L1
- §Ref: —

**B-04 — I2V Input Format (MUAPI)**

Veo image-to-video via the Hypogeum MUAPI path uses the `images_list` array format.

- Evidence: VEO-HYP-01
- Level: L1 (configuration fact, not behavioral claim)
- §Ref: —

### 4.2 Supported Durations

**B-05 — Generation Duration Options**

Veo supports base generation of 4, 6, or 8 seconds. Extensions are available in up to 7-second increments via the video extension feature, up to a maximum assembled length of 148 seconds.

- Evidence: VEO-DOC-02
- Level: L1
- §Ref: §23.1

**Note:** Previous versions of this skill documented 8 seconds as the only supported duration. This was incorrect. The Hypogeum pipeline currently configures 8-second clips; this is a pipeline configuration choice, not a model constraint.

### 4.3 Supported Aspect Ratios

**B-06 — Aspect Ratio Support**

Confirmed supported aspect ratios: 16:9 (1280×720 landscape) and 9:16 (720×1280 portrait).

- Evidence: VEO-DOC-03
- Level: L1
- §Ref: —

### 4.4 Audio Behavior

**B-07 — Audio Types (Three Categories)**

Official documentation defines three audio element types:

1. **Sound effects** — individual, distinct sounds: `"the sound of a phone ringing"`, `"water splashing in the background"`
2. **Ambient noise** — general background soundscape: `"the sounds of city traffic and distant sirens"`, `"waves crashing on the shore"`, `"the quiet hum of an office"`
3. **Dialogue** — spoken words: `A woman says, "We have to leave now."`, `"a voiceover with a polished British accent speaks in a serious, urgent tone"`

- Evidence: VEO-DOC-01
- Level: L2 (documented categorization and syntax framework — behavioral effectiveness of category control is L5)
- §Ref: §21.1, §21.2

**B-08 — Audio Declaration Syntax**

Official documentation specifies prompt syntax for each audio type:

- Dialogue: use quotation marks → `The man says, "Where is the rabbit?"`
- Sound effects: use `SFX:` prefix → `SFX: thunder cracks in the distance`
- Ambient: use `Ambient noise:` prefix → `Ambient noise: the quiet hum of a starship bridge`
- Music/orchestral: describe in prompt text → `Background music: grand orchestral score building to climax`

- Evidence: VEO-DOC-01, VEO-DOC-02
- Level: L2 (documented syntax — whether these prefixes reliably control output category is L5)
- §Ref: §21.3

### 4.5 Officially Supported Camera Vocabulary

**B-09 — Camera Angles (Official List)**

The following camera angles are listed in official documentation:

| Angle | Description |
|---|---|
| Eye-level shot | Neutral human-height perspective |
| Low-angle shot | Camera below subject looking up |
| High-angle shot | Camera above subject looking down |
| Bird's-eye view / top-down shot | Directly from above |
| Worm's-eye view | Looking straight up from the ground |
| Dutch angle / canted angle | Camera tilted to one side — conveys unease/disorientation |
| Close-up | Tight framing, typically face |
| Extreme close-up | Isolates a very small detail |
| Medium shot | Subject approximately waist up |
| Full shot / long shot | Entire subject with some environment |
| Wide shot / establishing shot | Subject within broad environment |
| Over-the-shoulder shot | From behind one person, looking over shoulder at another |
| Point-of-view shot (POV) | Direct visual perspective of a character |

Official documentation note: "Some advanced camera angles are not officially supported. Results and reliability may vary."

- Evidence: VEO-DOC-01
- Level: L2 (these terms appear in official documentation — behavioral reliability when used in prompts is L5 pending L4 testing)
- §Ref: §11.1, §12.1

**Important channel policy note:** Dutch angle (canted horizon) is listed in official Veo documentation. It is prohibited by the channel's style system (§11.3) for aesthetic reasons — this is a channel rule, not a provider limitation. Do not describe dutch angle as unsupported by Veo.

**B-10 — Camera Movements (Official List)**

The following camera movements are listed in official documentation:

| Movement | Description |
|---|---|
| Static shot (fixed) | Camera remains completely still |
| Pan (left/right) | Camera rotates horizontally from fixed position |
| Tilt (up/down) | Camera rotates vertically from fixed position |
| Dolly (in/out) | Camera physically moves closer or further |
| Truck (left/right) | Camera moves horizontally sideways, parallel to subject |
| Pedestal (up/down) | Camera moves vertically while maintaining level perspective |
| Zoom (in/out) | Lens changes focal length — camera does not move |
| Crane shot | Camera on crane; vertical or sweeping arc movement |
| Aerial shot / drone shot | High altitude, smooth flying movements |
| Handheld / shaky cam | Less stable, jerky — conveys realism or unease |
| Whip pan | Extremely fast pan that blurs image |
| Arc shot | Camera moves in circular or semi-circular path |

Additional vocabulary from official sources: dolly shot, tracking shot, slow pan, POV shot.

- Evidence: VEO-DOC-01, VEO-DOC-02
- Level: L2 (these terms appear in official documentation — behavioral reliability when used in prompts is L5 pending L4 testing)
- §Ref: §11.2

**B-11 — Lens and Optical Effects (Official List)**

| Effect | Notes |
|---|---|
| Wide-angle lens | Broader FOV, exaggerated perspective |
| Telephoto lens | Narrow FOV, compresses perspective |
| Shallow depth of field | Narrow focus plane; background blurred (bokeh) |
| Deep depth of field | Most/all of image in sharp focus |
| Lens flare | Light striking lens |
| Rack focus | Shifting focus from one plane to another |
| Fisheye lens effect | Ultra-wide, extreme barrel distortion |
| Vertigo effect (dolly zoom) | Camera dollies + zoom goes opposite direction |

- Evidence: VEO-DOC-01
- Level: L2 (documented vocabulary — behavioral reliability is L5 pending L4 testing)
- §Ref: §12.2

### 4.6 Officially Supported Prompt Syntax

**B-12 — Timestamp Prompting (Multi-Shot Sequencing)**

Official documentation describes timestamp-based prompting for directing multi-shot sequences within a single generation:

```
[00:00-00:02] Medium shot from behind a young explorer...
[00:02-00:04] Reverse shot of the explorer's face...
[00:04-00:06] Tracking shot following the explorer...
[00:06-00:08] Wide, high-angle crane shot...
```

This is the official syntax for expressing temporal structure. Previous Hypogeum practice used `0-4s:` / `4-8s:` format — this is not the officially documented syntax.

- Evidence: VEO-DOC-01
- Level: L2 (documented syntax — whether the model reliably honors segment boundaries is L5 pending L4 testing)
- §Ref: §19.2

**B-13 — Reference Image Input (Ingredients to Video)**

Veo 3.1 supports up to 3 reference images for character/style/setting consistency. Referenced via the `reference_images` parameter in the API. Characters can be referred to by consistent name in the prompt text.

- Evidence: VEO-DOC-01, VEO-DOC-03
- Level: L1 (capability fact — reference image input is supported)
- §Ref: —

### 4.7 Negative Constraint Format

**B-14 — Official Negative Prompt Guidance**

Official documentation explicitly states: "We recommend that you don't use instructive language such as 'no' or 'don't'."

**NOT recommended (official):** `"no walls"`, `"don't show walls"`, `[no vignette]`, `[no frontal faces]`

**Recommended (official):** Describe what to exclude as nouns or descriptors:
- Correct: `"wall, frame"` (nouns listing what to exclude)
- Correct: `"a desolate landscape with no buildings or roads"` (descriptive exclusion built into positive description)
- Correct: `"urban background, man-made structures"` (descriptor-form negative)

- Evidence: VEO-DOC-01, VEO-DOC-02
- Level: L2 (documented recommendation — whether descriptor-form is more effective than "no X" is L5 pending L4 testing)
- §Ref: §17.1, §18

**Critical correction from previous skill versions:** Previous versions of this skill used `[no vignette]`, `[no frontal faces]`, `[no ambient fill]`, etc. throughout. This format is NOT recommended by official documentation. The translation tables in §7 have been updated to use the officially recommended approach. Items that previously relied on `[no X]` format are now marked with evidence status accordingly.

---

## 5. Prompt Authoring Guidance (Section B)

**Scope:** Provider-endorsed techniques for writing effective prompts. Sources: Official Documentation (L2), Official Repositories (L2), approved Community Skills (L3). Generic video prompting repositories (VEO-GEN-01) are permitted here as general technique sources but not as evidence of Veo-specific behavior.

### 5.1 Recommended Prompt Structure — Official Five-Part Formula

Official documentation defines a five-part structure for Veo prompts:

```
[Cinematography] + [Subject] + [Action] + [Context] + [Style & Ambiance]
```

**Official example:**
> "Medium shot, a tired corporate worker, rubbing his temples in exhaustion, in front of a bulky 1980s computer in a cluttered office late at night. The scene is lit by harsh fluorescent overhead lights and the green glow of the monochrome monitor. Retro aesthetic, shot as if on 1980s color film, slightly grainy."

All five parts are optional — they represent the recommended structure, not a rigid requirement.

- Evidence: VEO-DOC-01, VEO-DOC-02
- Level: L2

**Hypogeum production ordering** (from prior skill — L5, no official backing): Shot scale → primary subject → lighting → material → environment → camera → motion → tags → constraints. This ordering is not officially specified and should be treated as an internal convention only.

### 5.2 Camera Language Authoring

Use the official camera vocabulary from §4.5 (B-09, B-10). Prefer specific official terms over informal paraphrases.

**Pattern from official docs:** Camera movement is specified as part of the cinematography element, with associated speed or style qualifiers.

**Official examples:** "Dolly shot", "tracking shot", "crane shot", "aerial view", "slow pan", "POV shot"

- Evidence: VEO-DOC-01, VEO-DOC-02
- Level: L2 (documented vocabulary; that the model reliably executes these as cinematic movements is L5)

**Stacking movements:** Official documentation does not explicitly prohibit stacking movements, but the five-part formula implies one primary movement per cinematography specification. Stacking is classified as L5 risk in Section C.

### 5.3 Audio Declaration Authoring

Official guidance: "Clearly specify if you want audio. We recommend that you use separate sentences in your prompt to describe the audio."

**For channel production (ambient-only intent):**

Describe permitted audio content as a positive statement of what the space naturally sounds like, using the official three-category framework:

- Ambient: `Ambient noise: natural stone acoustic resonance, distant settlement sounds of the structure.`
- Sound effects exclusion (descriptor form, not "no X"): `sound design: structural ambience only, speech absent, music absent`
- Explicit declaration: Use separate sentence(s) for audio intent, as officially recommended

**Note on suppression:** The official documentation does not describe a mechanism for fully suppressing audio generation. The channel's requirement (§21.3) for no music, no speech, no narration is a strict constraint. The best-known prompt approach uses the official audio declaration syntax to declare ambient-only intent, but whether this fully suppresses inferred audio is L5 pending L4 testing (see V-05).

- Evidence: VEO-DOC-01 (audio declaration structure), VEO-DOC-02
- Level: L2 for declaration syntax; L5 for ambient-only suppression effectiveness

### 5.4 Negative Constraint Authoring — Official Approach

Use the officially recommended descriptor-form exclusion rather than `[no X]` phrasing:

| Channel requirement | Official negative constraint approach |
|---|---|
| No modern elements | Build into environmental description: `ancient stone corridor, period-accurate materials, no contemporary infrastructure` |
| No frontal human faces | Descriptor approach: `figures seen from behind, silhouetted against the light source` |
| No artificial post-processing effects | Describe the desired visual register positively: `photographic documentary register, practical light sources` |
| No even/flat illumination | Positive description of desired contrast: `directional key light with hard falloff, deep shadow zones` |

- Evidence: VEO-DOC-01, VEO-DOC-02
- Level: L2 for recommended format; L5 for effectiveness of each specific application

### 5.5 Subject, Action, and Context Authoring

Official documentation specifies the following vocabulary for subject, action, and context elements:

**Subject types:** people (generic or specific), animals/creatures, objects, multiple subjects

**Action vocabulary (officially listed):**
- Basic: walking, running, jumping, flying, swimming, dancing, spinning, falling, standing still, sitting
- Interactions: talking, laughing, arguing, hugging, fighting, playing, cooking, building
- Subtle: "a gentle breeze ruffling hair", "leaves rustling", "fingers tapping impatiently"
- Transformations: "a flower blooming in fast-motion", "ice melting"

**Context (scene/location) elements:** interior/exterior locations, time of day (golden hour, midday, twilight, deep night), weather, historical/fantastical periods, atmospheric details

**Lighting vocabulary officially listed:** "soft morning sunlight", "warm glow of a fireplace", "harsh fluorescent office lighting", "rembrandt lighting", "film noir style with deep shadows", "high-key lighting", "low-key lighting", "volumetric lighting creating visible light rays"

- Evidence: VEO-DOC-01
- Level: L2

### 5.6 Style Descriptor Authoring

Official style categories for the ambiance element:

- Photorealistic: `"ultra-realistic rendering"`, `"shot on 8K camera"`
- Cinematic: `"cinematic film look"`, `"shot on 35mm film"`, `"anamorphic widescreen"`
- Documentary: `"documentary register"` (not explicitly listed by name in official docs; photorealistic is the closest)
- Color palettes: `"monochromatic black and white"`, `"muted earthy tones"`, `"warm autumnal oranges and browns"`
- Texture: `"rough-hewn stone walls"`, `"smooth, polished chrome surfaces"`

- Evidence: VEO-DOC-01
- Level: L2

---

## 6. Hypogeum Production Experience (Section C)

**Scope:** Observations and hypotheses from Hypogeum's own production use of Veo. All items carry reduced authority. All behavioral claims are L5 (hypothesis) unless they represent a formally documented experiment qualifying for L4. No L4 items exist in this section — VEO-HYP-02 has no documented experiment IDs, prompts, or controlled outputs.

**Source for all items in this section:** VEO-HYP-02 unless otherwise noted.

### 6.1 Configuration Notes

- Hypogeum MUAPI routes to `veo3-text-to-video` (T1) and `veo3-fast-text-to-video` (T2/T3)
- Pipeline configures 8-second clip duration — this is a Hypogeum configuration choice; Veo supports 4, 6, and 8 seconds [L1: VEO-DOC-02]
- I2V path uses `images_list` array format [VEO-HYP-01, L1 — configuration fact]
- Veo3 Premium recorded generation latency of ~128.5s in Hypogeum benchmark

### 6.2 Observations from Production Testing

**C-01 — Front-Weighted Prompt Processing**

Observation: Elements described earlier in the prompt appear to receive more weight than later elements. Subject, shot scale, and primary lighting declared early produce more consistent outputs than when the same elements appear late.

- Evidence: VEO-HYP-02
- Level: L5 (no controlled methodology; no experiment ID)
- §Ref: §33.1

**C-02 — Single Movement Type Reliability**

Observation: Single camera movement verbs produce more consistent and repeatable outputs than prompts containing two concurrent movement instructions. When stacked, the model appears to favor one movement and diminish the other.

- Evidence: VEO-HYP-02
- Level: L5 (observed informally; no controlled test)
- §Ref: §11.2

**C-03 — Physical Specificity vs. Mood Language**

Observation: Prompts describing physical phenomena (named light sources, specific material behaviors, named atmospheric elements) appear to produce more specific and controlled outputs than prompts using mood or emotional language.

- Evidence: VEO-HYP-02; general consistency with official examples which use physical description language
- Level: L5 (not formally tested; consistency with official examples does not promote to L2)
- §Ref: §8, §10, §16

**C-04 — Centre-Frame Default for Close/Medium Shots**

Observation: Without explicit compositional instruction, primary subjects tend to appear in the center of the frame in close-up and medium shots.

- Evidence: VEO-HYP-02
- Level: L5
- §Ref: §13.1

**C-05 — Material Description Response**

Observation: Describing material surfaces with period-specific surface condition, aging state, and light behavior appears to produce more forensic surface detail than generic material names.

- Evidence: VEO-HYP-02
- Level: L5
- §Ref: §16.1

**C-06 — Atmospheric Anchoring**

Observation: Atmospheric elements (smoke, dust, moisture) appear to distribute more specifically when anchored to a light source location and a frame zone, rather than described as filling the scene generally.

- Evidence: VEO-HYP-02
- Level: L5
- §Ref: §10.1

**C-07 — Default Vignette**

Observation: Veo appears to apply a post-processing vignette to some outputs. The `[no vignette]` tag was used in prior Hypogeum production. However, official guidance recommends against `[no X]` phrasing (B-14), so the suppression mechanism is unclear.

- Evidence: VEO-HYP-02 (observation); VEO-DOC-01 (official guidance against `[no X]` format, L2)
- Level: L5 — the vignette observation is informal; the suppression approach is unconfirmed
- §Ref: —

**C-08 — Speed Qualifier Effect**

Observation: Attaching speed qualifiers to camera movement verbs (`controlled slow push-in` vs. `push-in`) appears to produce more deliberate, intentional motion. Speed qualifiers are not mentioned in official documentation as a required or recommended element.

- Evidence: VEO-HYP-02
- Level: L5
- §Ref: §11.2

### 6.3 Hypotheses Under Investigation

| Hypothesis | Level | See |
|---|---|---|
| `[no X]` format may be effective despite not being officially recommended | L5 | V-07 |
| Falloff language controls tonal range more effectively than other approaches | L5 | C-03 |
| Kelvin values in prompt text influence output color temperature | L5 | V-04 |
| Background void appears without explicit environmental description | L5 | C-04 adjacent |

---

## 7. Translation Rules

Maps each canonical concept from `video_style_system.md` to its corresponding Veo prompt expression. Evidence basis is cited for each translation approach with level; approaches derived from Hypogeum hypothesis are marked [L5].

### 7.1 Color System (§7)

**Canonical:** §7 defines the channel identity palette with hex ranges for shadow, mid tones, key light, and cool accent.

**Evidence basis:** Veo does not expose a color hex parameter. Color emerges from light and material descriptions. This translation approach is derived from the official prompt structure (VEO-DOC-01) where color is expressed through lighting and style descriptors — not directly. The specific translation mappings below are L5; the general use of lighting/style descriptors for color is consistent with official examples (L2) but the specific expressions are Hypogeum-authored.

| Canonical Color Concept | Veo Prompt Expression | Level |
|---|---|---|
| Deep void shadow | `hard falloff to absolute darkness` / positive: `the opposing face in deep shadow` | [L5] |
| Bronze/aged metal | `bronze surface with oxidised brown-green patina, warmer copper at wear points` | [L5] |
| Amber/gold key light | `warm amber light`, `copper-amber torch warmth`, `warm golden raking light` | [L5] (vocabulary consistent with L2 lighting terms but specific mapping is hypothesis) |
| Cool atmospheric accent | `stone-grey reflected ambient`, `cool overcast diffuse from the opening above` | [L5] |
| Warm zones 35–50% of frame | Controlled through directional lighting description | [L5] |

### 7.2 Lighting System (§8)

**Canonical:** §8 requires a warm key light (motivated, directional, period-plausible) and a secondary cooler source (edge separation only).

**Evidence basis:** Official documentation lists Rembrandt lighting, film noir lighting, directional lighting, backlighting as vocabulary examples (L2). The specific translation expressions below (falloff language, entry direction as angle) are Hypogeum-authored and are L5.

| Canonical Requirement | Veo Prompt Expression | Level |
|---|---|---|
| Warm key light, period-plausible source | `warm amber light enters from the upper left, raking across the surface` | [L5] (vocabulary consistent with L2 lighting terms; specific mapping is hypothesis) |
| Sharp falloff | `with hard falloff into shadow` | [L5] |
| Secondary cooler source, subordinate | `a cooler secondary light provides edge separation without competing with the key` | [L5] |
| Flat frontal lighting forbidden (§8.4) | Descriptor approach: `directional key light from one side, deep shadow on the opposing face` | [L5] (approach consistent with L2 film noir vocabulary; effectiveness is hypothesis) |
| Uniform ambient forbidden | Positive description: `focused directional light, shadow zones present` | [L5] |
| Lamp source off-frame | [NOT OFFICIALLY DOCUMENTED] — L5: positional language `light source above and left, not visible in frame` | [L5] |

### 7.3 Contrast and Shadow (§9)

**Canonical:** §9 requires high directional contrast, true near-black shadows, warm amber highlights.

**Evidence basis:** Official documentation describes "film noir style with deep shadows and stark highlights" and "dramatic side lighting" as available vocabulary (L2). The specific translation expressions are L5.

| Canonical Requirement | Veo Prompt Expression | Level |
|---|---|---|
| True near-black shadows | `deep shadow zones`, `hard falloff into darkness` | [L5] |
| Warm amber highlights | `warm amber catch light at the specular peak` | [L5] |
| Shadow edge character | `semi-hard shadow edge at the key light boundary` | [L5] |

### 7.4 Atmosphere (§10)

**Canonical:** §10 requires atmospheric evidence of physical inhabitation in every scene.

**Evidence basis:** Official documentation lists atmospheric details as part of scene/context: "floating dust motes in a sunbeam", "shimmering heat haze", "reflections on wet pavement" (L2). These examples confirm atmospheric vocabulary is used in official prompts. The specific channel expressions below are L5.

| Canonical Atmospheric Type | Veo Prompt Expression | Level |
|---|---|---|
| Underground suspended particulate | `stone dust in the light beam, tracing its angle` | [L5] |
| Combustion haze | `torch smoke drifting upward, accumulating at the vault ceiling` | [L5] |
| Moisture on stone | `moisture condensation beaded across the stone face` | [L5] |
| Exterior dust | `airborne excavation dust diffusing the near-field light` | [L5] |

### 7.5 Camera Language (§11)

**Canonical:** §11 requires single motivated camera movement per scene (§11.2), prohibits dutch tilt for this channel (§11.3), restricts handheld to POV exploration.

**Evidence basis:** Official camera movement vocabulary is L2 [VEO-DOC-01] — these terms appear in official documentation. Channel restrictions are policy (§11.3), not provider limitations. That the model reliably executes these as cinematic movements is L5.

| Canonical Narrative Function | Veo Prompt Expression | Level |
|---|---|---|
| Discovering a subject | `slow dolly in` / `slow push-in` | [L2] (term documented) |
| Withdrawing from subject | `dolly out` / `pull back` | [L2] (term documented) |
| Surveying a surface | `slow pan` / `truck shot` | [L2] (term documented) |
| Descending into space | `crane shot descending` / `pedestal down` | [L2] (term documented) |
| Ascending | `crane shot ascending` / `pedestal up` | [L2] (term documented) |
| Encircling for three-dimensionality | `arc shot around the subject` | [L2] (term documented) |
| POV exploration | `POV shot` / `handheld` | [L2] (term documented) |
| Establishing hold | `static shot` | [L2] (term documented) |

**Level note for camera vocabulary:** [L2] means the term appears in official documentation. Whether the model reliably executes these as the intended cinematic movement in practice requires L4 evidence (controlled experiment with documented outputs). Until L4 testing is completed, behavioral reliability is L5.

**Suppression of §11.3 dutch tilt prohibition:** Dutch angle is listed in official Veo documentation [L2]. Channel policy prohibits it. Use descriptor approach: `"level camera horizon"` as part of the cinematography description. [L5 for suppression effectiveness — V-08]

**Stacking movements:** Not explicitly prohibited by official docs but L5 risk based on C-02. Official structure implies one movement per prompt element.

### 7.6 Lens Language and Shot Scale (§12)

**Canonical:** §12 defines nine permitted shot scales and depth of field requirements.

**Evidence basis:** Official camera angle vocabulary is L2 [VEO-DOC-01] — these terms appear in official documentation. Shot scale terms match official list.

| Canonical Shot Scale | Veo Prompt Term | Level |
|---|---|---|
| Extreme close-up | `extreme close-up` | [L2] (term documented) |
| Close-up | `close-up` | [L2] (term documented) |
| Medium close-up | `medium close-up` | [L2] (term documented) |
| Medium | `medium shot` | [L2] (term documented) |
| Wide establishing | `wide shot` / `establishing shot` | [L2] (term documented) |
| Overhead | `top-down shot` / `bird's-eye view` | [L2] (term documented) |
| Low angle | `low-angle shot` | [L2] (term documented) |

**Depth of field:** Official vocabulary lists `shallow depth of field` and `deep depth of field`. Use these terms directly. [L2: VEO-DOC-01]

### 7.7 Composition and Spatial Layering (§13)

**Canonical:** §13 requires primary subject in right 40–60% of frame, minimum two spatial layers.

**Evidence basis:** [NOT OFFICIALLY DOCUMENTED] — no official documentation describes compositional positioning behavior. All translation approaches in this subsection are L5.

| Canonical Requirement | Veo Prompt Expression | Level |
|---|---|---|
| Primary subject right 40–60% | `subject positioned in the right half of the frame` | [L5] |
| Minimum two spatial layers | Describe both layers physically | [L5] |

### 7.8 Subject Scale (§14)

**Canonical:** §14 defines monumental (45–65% of frame height) and scale contrast (5–15%) approaches.

**Evidence basis:** Scale is influenced through shot scale selection [L2: VEO-DOC-01] combined with subject description. Fine-grained percentage targets are L5.

| Canonical Approach | Veo Prompt Expression | Level |
|---|---|---|
| Monumental | Close-up / medium close-up + subject fills vertical frame | [L5] (shot scale term is L2; frame percentage control is L5) |
| Scale contrast | Wide shot + small figure in environment | [L5] (shot scale term is L2; scale percentage control is L5) |
| Scale anchor | Name a recognizable-scale reference object | [L5] |

### 7.9 Environmental Storytelling (§15)

**Canonical:** §15 prohibits void backgrounds; every subject must exist in a plausible physical environment.

**Evidence basis:** Official context vocabulary includes interior/exterior locations, atmospheric details (L2). Physical environment description is consistent with official guidance. The specific channel expressions below are L5.

| Canonical Requirement | Veo Prompt Expression | Level |
|---|---|---|
| No void backgrounds | Name at least one environmental element: `[architectural feature] visible in the background` | [L5] |
| Spatial evidence | `worn stone at the threshold`, `soot darkening the vault above` | [L5] |

### 7.10 Material Authenticity (§16)

**Canonical:** §16 requires at least one period-specific material observation per scene, with age and use history visible.

**Evidence basis:** Official documentation lists "rough-hewn stone walls", "smooth, polished chrome surfaces", "dewdrops clinging to a spiderweb" as texture vocabulary (L2). Period-specific material description is consistent with the official approach but the specific channel expressions are L5.

| Period Material | Veo Prompt Expression | Level |
|---|---|---|
| Travertine | `travertine surface, raking light catching raised topography, shadow in recessed joints` | [L5] |
| Roman concrete | `opus incertum concrete, irregular aggregate in thick mortar, warm light on protruding faces` | [L5] |
| Age indicators | `mineral staining, soot darkening, root penetration at joints` | [L5] |

### 7.11 Historical Authenticity — Anachronism Suppression (§17)

**Canonical:** §17 prohibits modern light sources, construction materials, contemporary human elements, frontal faces (§17.3).

**Evidence basis:** Negative constraints must use the official descriptor approach [B-14, L2: VEO-DOC-01]. Whether descriptor-form exclusion reliably suppresses described elements is L5.

| Channel Requirement | Official Descriptor Approach | Level |
|---|---|---|
| No modern light sources | `period-accurate light sources only, practical torches and oil lamps` | [L2] for format; [L5] for suppression effectiveness |
| No modern infrastructure | `ancient stone construction, period-accurate materials` | [L2] for format; [L5] for suppression effectiveness |
| No frontal faces (§17.3) | `figures seen from behind or in silhouette` | [L2] for format; [L5] for suppression effectiveness |

### 7.12 Realism — CGI and Illustration Suppression (§18)

**Canonical:** §18 prohibits CGI appearance, illustration register, game-engine appearance.

**Evidence basis:** Official style vocabulary includes "ultra-realistic rendering", "shot on 8K camera" (L2). Documentary/photorealistic register is achievable through official style descriptors. Whether these terms reliably suppress CGI appearance is L5.

| Channel Requirement | Veo Prompt Expression | Level |
|---|---|---|
| Photorealistic / documentary register | `documentary register, photorealistic, practical light sources` | [L2] for vocabulary; [L5] for CGI suppression effectiveness |
| No CGI appearance | Positive description of physical surface behavior | [L5] |
| No illustration register | `photorealistic` as style descriptor | [L2] for vocabulary; [L5] for effectiveness |

### 7.13 Motion and Temporal Structure (§19)

**Canonical:** §19 requires physical change within each clip, at least one secondary motion element, and camera energy progression (§19.4).

**Evidence basis:** Official timestamp prompting [B-12, L2: VEO-DOC-01] provides the temporal structure syntax. Secondary motion and physical specificity are L5.

| Canonical Requirement | Veo Prompt Expression | Level |
|---|---|---|
| Temporal structure in 8s clip | `[00:00-00:04] [opening state]. [00:04-00:08] [physical change from opening state].` | [L2] for timestamp syntax; [L5] for whether segment boundaries are reliably honored |
| Secondary motion element | Name it in the prompt after camera movement | [L5] |
| Phase 1 establishes | Describe the physical state the viewer enters | [L5] |
| Phase 2 develops | Describe the specific physical change | [L5] |

**Note on temporal format:** Previous Hypogeum practice used `0-4s:` / `4-8s:` format. The official documented format is `[00:00-00:04]`. CPA should migrate to the official format.

### 7.14 Audio Policy (§21)

**Canonical:** §21 requires explicit audio intent declaration; natural environmental ambience only; no music, speech, narration, or modern audio.

**Evidence basis:** Official audio declaration mechanism is L2 [B-07, B-08: VEO-DOC-01]. The `Ambient noise:` prefix is documented syntax. Whether these prefixes and declarations reliably control audio output to ambient-only is L5 pending L4 testing (V-05).

| Canonical Audio Requirement | Veo Prompt Expression | Level |
|---|---|---|
| Explicit audio intent declaration | Dedicated audio sentence(s), as officially recommended | [L2] for recommendation; [L5] for behavioral effectiveness |
| Natural ambient only | `Ambient noise: natural [environment] acoustics` | [L2] for syntax; [L5] for ambient-only effectiveness |
| No music | Audio absence phrased as positive description of ambient: `ambient environmental sound only` | [L2] for syntax; [L5] for suppression effectiveness (REQUIRES VERIFICATION: V-05) |
| No speech / narration | `sound design: ambient only, no speech, no narration` or descriptor approach | [L5] (REQUIRES VERIFICATION: V-05) |

**Current best-known approach:** Use a separate audio sentence in the prompt: `Ambient noise: natural stone acoustics and distant environmental sounds consistent with the physical space. No music. No speech. No narration.` This uses the official `Ambient noise:` prefix and descriptor-form exclusions.

### 7.15 Scene Narrative Focus (§33)

**Canonical:** §33 requires one primary narrative focus and one primary visual action per scene.

**Evidence basis:** Official 5-part formula implies one primary subject and one primary action in the subject + action elements [L2: VEO-DOC-01]. Fine-grained Veo behavioral fidelity to this structure is L5.

| Canonical Focus Rule | Veo Prompt Expression | Level |
|---|---|---|
| One primary subject | Name the primary subject once in the subject element | [L2] for formula structure; [L5] for behavioral fidelity |
| One primary visual action | One action verb in the action element | [L2] for formula structure; [L5] for behavioral fidelity |

### 7.16 Cross-Episode Visual Continuity (§34)

**Canonical:** §34 requires consistent color temperature, atmospheric register, and lighting character across all clips in the same physical setting within an episode.

**Evidence basis:** Veo generates each clip independently — no inter-generation memory. Consistency is achieved through vocabulary discipline, not through a provider mechanism. [VEO-HYP-02, L5]

**Translation approach:** Reuse identical vocabulary anchors across all same-setting clips. If `warm amber light` is used in clip 1, every subsequent clip in the same setting uses `warm amber light`, not `golden light` or `orange warmth`.

- Evidence: VEO-HYP-02
- Level: L5 — translation discipline maintained through vocabulary consistency; no provider mechanism exists

---

## 8. Rule Traceability Table

| Rule (§7 subsection) | Evidence IDs | §Ref | Level |
|---|---|---|---|
| 7.1 Color System | VEO-DOC-01 (lighting vocabulary, L2), VEO-HYP-02 | §7 | L5 for all specific color translations |
| 7.2 Lighting System | VEO-DOC-01 (lighting vocabulary, L2), VEO-HYP-02 | §8 | L5 for all specific expressions; L2 for use of official lighting term vocabulary |
| 7.3 Contrast and Shadow | VEO-DOC-01 (film noir vocabulary, L2), VEO-HYP-02 | §9 | L5 |
| 7.4 Atmosphere | VEO-DOC-01 (atmospheric vocabulary, L2), VEO-HYP-02 | §10 | L5 for specific expressions; consistent with L2 vocabulary |
| 7.5 Camera Language | VEO-DOC-01 (movement terms, L2) | §11 | L2 for vocabulary presence in docs; L5 for behavioral reliability |
| 7.6 Lens Language and Shot Scale | VEO-DOC-01 (angle terms, L2) | §12 | L2 for vocabulary presence; L5 for DoF behavioral effect |
| 7.7 Composition and Spatial Layering | None | §13 | L5 |
| 7.8 Subject Scale | VEO-DOC-01 (shot scale L2), VEO-HYP-02 | §14 | L2 for shot scale terms; L5 for frame percentage control |
| 7.9 Environmental Storytelling | VEO-DOC-01 (context vocabulary, L2), VEO-HYP-02 | §15 | L5 |
| 7.10 Material Authenticity | VEO-DOC-01 (texture vocabulary, L2), VEO-HYP-02 | §16 | L5 |
| 7.11 Anachronism Suppression | VEO-DOC-01 (negative format guidance, L2) | §17 | L2 for descriptor format; L5 for suppression effectiveness |
| 7.12 CGI Suppression | VEO-DOC-01 (style vocabulary, L2) | §18 | L2 for photorealistic vocabulary; L5 for CGI suppression effectiveness |
| 7.13 Motion and Temporal Structure | VEO-DOC-01 (timestamp format, L2) | §19 | L2 for timestamp syntax; L5 for behavioral segment fidelity |
| 7.14 Audio Policy | VEO-DOC-01 (audio declaration syntax, L2) | §21 | L2 for syntax; L5 for ambient-only suppression effectiveness (REQUIRES VERIFICATION: V-05) |
| 7.15 Scene Narrative Focus | VEO-DOC-01 (5-part formula, L2) | §33 | L2 for formula structure; L5 for behavioral fidelity |
| 7.16 Cross-Episode Continuity | VEO-HYP-02 | §34 | L5 — translation discipline, no provider mechanism |

---

## 9. Prompt Anti-Patterns

### AP-01 — Stacked Camera Movements

**Pattern:** Two camera movement types in one prompt. `dolly in while panning right`, `orbit with a simultaneous tilt`.

**Symptom:** Unpredictable output — model may favor one movement and ignore the other, or produce neither clearly. [L5: C-02 — no official documentation of this behavior]

**Fix:** One camera movement term per prompt, derived from the scene's single narrative function (§11.2).

**Evidence:** VEO-HYP-02 [Level: L5]

---

### AP-02 — Contradictory Adjectives

**Pattern:** Opposing descriptors for the same quality. `dynamic but subtle`, `warm but with a cool feel`.

**Symptom:** Undifferentiated output that satisfies neither direction.

**Fix:** Commit to one direction; express nuance as two distinct, non-competing elements.

**Evidence:** VEO-HYP-02 [Level: L5]

---

### AP-03 — Mood Vocabulary Without Physical Anchor

**Pattern:** `mysterious and ancient`, `dramatic and powerful`, `hauntingly beautiful`.

**Symptom:** Generic, clichéd output lacking physical specificity.

**Fix:** Replace mood adjectives with physical observations. The mood emerges from physical description; it is not described as a target.

**Evidence:** VEO-HYP-02 [Level: L5]; consistent with L2 vocabulary — official examples use physical descriptions throughout

---

### AP-04 — Generic Material Description

**Pattern:** `stone`, `metal`, `wood`, `old brickwork` without surface behavior description.

**Symptom:** Generic, smooth, uniformly lit surfaces lacking period specificity.

**Fix:** Include surface condition, aging state, and light behavior for every named material (§7.10).

**Evidence:** VEO-HYP-02 [Level: L5]

---

### AP-05 — Audio Intent Omission

**Pattern:** Submitting a prompt with no audio sentence when the scene contains elements that Veo will use to infer audio (crowds, mechanisms, water, fire, ceremonial activity).

**Symptom:** Veo generates inferred audio (crowd noise, music, sound effects) that competes with the pipeline's narration and music tracks.

**Fix:** Every prompt must include a dedicated audio declaration sentence (§7.14, §21.3). Omission is a production failure.

**Evidence:** VEO-DOC-01 [Level: L1 — audio generation is default; L2 — declaration is recommended]

---

### AP-06 — Using `[no X]` Negative Constraint Format

**Pattern:** `[no vignette]`, `[no frontal faces]`, `[no ambient fill]`, `[without modern elements]`.

**Symptom:** Official documentation explicitly recommends against `no` or `don't` phrasing. Effectiveness of this format is L5 — unverified and against official guidance.

**Fix:** Use the officially recommended descriptor approach: describe what to exclude as nouns or descriptors integrated into positive description (§5.4, B-14).

**Evidence:** VEO-DOC-01, VEO-DOC-02 [Level: L2 — official recommendation against "no X" phrasing]

---

### AP-07 — Mood-Based Lighting Without Source

**Pattern:** `warm atmospheric lighting fills the space`, `dramatic shadows`, `golden light bathes the scene`.

**Symptom:** Diffuse, directionless output inconsistent with the channel's sharp motivated lighting character.

**Fix:** Every lighting description must name a source character, direction, what it illuminates, and how it falls off (§7.2).

**Evidence:** VEO-HYP-02 [Level: L5]

---

## 10. Provider Mapping Table

| §Ref | Rule | Prompt Requirement | Explicit Expression Required |
|---|---|---|---|
| §7.1 | Color palette | IMPLICIT | Colors emerge from light + material descriptions [L5] |
| §7.2 | Warm/cool balance | SEMI-EXPLICIT | Through directional lighting and secondary source description [L5] |
| §7.4 | Forbidden colors | EXPLICIT | Descriptor approach in positive environmental description [L2 format; L5 effectiveness] |
| §8.1 | Two light sources | EXPLICIT | Both sources named with character and direction [L5] |
| §8.2 | Key light character | EXPLICIT | Source character, direction, named illuminated surface [L5] |
| §8.3 | Secondary source | EXPLICIT | Secondary source named with subordinate role [L5] |
| §8.4 | Forbidden lighting | EXPLICIT | Positive description of directional contrast — descriptor approach [L2 format; L5 effectiveness] |
| §9.1 | Shadow blacks | EXPLICIT | `hard falloff into darkness` / deep shadow in descriptor form [L5] |
| §10.1 | Atmospheric evidence | EXPLICIT | Named physical atmospheric phenomenon with frame location [L5] |
| §11.1 | Camera height | EXPLICIT | Official shot scale / angle vocabulary [L2 vocabulary] |
| §11.2 | Motivated movement | EXPLICIT | One official camera movement term + qualifier per prompt [L2 vocabulary; L5 reliability] |
| §11.3 | Dutch tilt forbidden (channel rule) | EXPLICIT | Descriptor: `level camera horizon` — [L5] for suppression effectiveness |
| §11.4 | Maximum hold | PLANNING | Scene planning stage — no prompt expression |
| §12.1 | Shot scale | EXPLICIT | Official shot scale vocabulary [L2] |
| §12.2 | Depth of field | EXPLICIT | Official: `shallow depth of field` / `deep depth of field` [L2] |
| §13.1 | Subject placement | EXPLICIT | Positional language — [L5] |
| §13.3 | Spatial layers | EXPLICIT | Physical element description at each layer — [L5] |
| §14.1 | Scale approach | EXPLICIT | Shot scale [L2] + subject scale language [L5] |
| §14.2 | Scale anchor | EXPLICIT | Named reference element — [L5] |
| §15.1 | Environmental context | EXPLICIT | Physical environment description in context element |
| §16.1 | Material specificity | EXPLICIT | Named material + surface condition + light behavior — [L5] |
| §16.2 | Material age | EXPLICIT | Age indicators in material description — [L5] |
| §17.1 | Anachronism prohibition | EXPLICIT | Descriptor-form exclusion — [L2] format; [L5] effectiveness |
| §17.3 | Face prohibition | EXPLICIT | Descriptor: `figures from behind or in silhouette` — [L2] format; [L5] effectiveness |
| §18.1–§18.3 | CGI prohibition | IMPLICIT + EXPLICIT | `photorealistic` / `documentary register` — [L2] vocabulary; [L5] effectiveness |
| §19.2 | Temporal structure | EXPLICIT | Official timestamp format: `[00:00-00:04] / [00:04-00:08]` [L2] |
| §19.3 | Secondary motion | EXPLICIT | Named in motion context — [L5] |
| §21.1 | Permitted audio | EXPLICIT | `Ambient noise:` prefix sentence [L2 syntax; L5 suppression effectiveness] |
| §21.2 | Prohibited audio | EXPLICIT | Descriptor-form exclusion in audio sentence — [L5] (REQUIRES VERIFICATION: V-05) |
| §21.3 | Audio intent declaration | EXPLICIT | Dedicated audio sentence [L2 recommendation; L5 behavioral effect] |
| §23.1 | 8-second duration | GENERATION PARAMETER | Pipeline sets 8s; Veo also supports 4s and 6s [L1] |
| §27.1 | Pre-generation validation | VALIDATION GATE | Applied before submission |
| §33.1 | One primary focus | SEMI-EXPLICIT | One subject + one action in 5-part formula [L2 structure; L5 fidelity] |
| §34.1 | Episode-level continuity | TRANSLATION DISCIPLINE | Vocabulary anchors reused across same-setting clips [L5] |

---

## 11. Verified Examples

### Example 1 — Lighting: Poor vs. Canonical

**POOR:**
> *"The ancient Roman vault is lit dramatically with warm atmospheric lighting, casting mysterious shadows."*

**Why it fails:** Mood vocabulary without physical source. No direction, no falloff, no named illuminated surface. [L5 observation — consistent with official examples which use physical description language]

**CANONICAL:**
> *"Cinematography: close-up. Warm amber light enters from the upper left, raking across the travertine vault face; directional falloff into shadow on the opposing side; secondary stone-grey ambient from above provides edge separation. Photorealistic documentary register."*

**Why it works:** Follows the official 5-part formula (cinematography + context lighting); uses official lighting vocabulary [L2: VEO-DOC-01]. Specific falloff expressions are L5 best practices.

**Evidence:** VEO-DOC-01 [L2 for formula and lighting vocabulary; L5 for specific translation expressions]

---

### Example 2 — Camera Movement: Poor vs. Canonical

**POOR:**
> *"The camera slowly orbits around the mechanism while gradually pushing in and panning slightly."*

**Why it fails:** Three concurrent movement instructions. The official formula implies one movement per cinematography specification. [L5: C-02; consistent with L2 official single-movement examples]

**CANONICAL:**
> *"Cinematography: arc shot, slow, around the mechanism. Subject: mechanism stationary under directional light. Torch smoke rising in the background."*

**Why it works:** One official camera movement term (`arc shot`), one speed qualifier, one narrative function. [L2: VEO-DOC-01 — `arc shot` is documented vocabulary]

**Evidence:** VEO-DOC-01 [Level: L2 for `arc shot` vocabulary]

---

### Example 3 — Material Description: Poor vs. Canonical

**POOR:**
> *"Old stone walls with some cracks and texture."*

**Why it fails:** Generic noun, no period specificity, no surface behavior. [L5 — consistent with L2 texture vocabulary examples using specific physical descriptions]

**CANONICAL:**
> *"Opus incertum concrete — irregular tuff aggregate in thick white lime mortar, joints wider than modern pointing; warm amber light catching the protruding aggregate faces; mortar joints in cooler shadow."*

**Why it works:** Period-specific material, physical construction description, named light behavior. Consistent with official texture vocabulary (`rough-hewn stone walls`, `dewdrops clinging to surface`). [L2: VEO-DOC-01 for texture vocabulary; L5 for specific period material expressions]

**Evidence:** VEO-DOC-01 [Level: L2 for texture vocabulary; L5 for specific expressions]

---

### Example 4 — Audio Declaration: Official Syntax

**POOR:**
> *[Prompt describes Roman workers operating a lifting mechanism — no audio sentence]*

**Why it fails:** Veo generates audio inferred from visual content by default. Without an audio sentence, crowd noise, mechanical sounds, and possibly music will appear in output, competing with the pipeline's narration track. [L1: VEO-DOC-01 — audio generation is default]

**CANONICAL:**
> *At end of prompt: `Ambient noise: natural stone acoustic resonance and distant structural settlement sounds. Sound design: ambient environmental audio only, speech absent, music absent.`*

**Why it works:** Uses the official `Ambient noise:` prefix syntax. Uses a dedicated audio sentence as officially recommended. Uses descriptor-form exclusion rather than `[no speech]`. [L2: VEO-DOC-01 for format; L5 for ambient-only suppression effectiveness — REQUIRES VERIFICATION: V-05]

**Evidence:** VEO-DOC-01 [Level: L1 for audio default; L2 for syntax and declaration recommendation]

---

## 12. Verification Queue

| # | Behavior | Section | Suggested Verification Method |
|---|---|---|---|
| V-01 | Whether stacked camera movements consistently produce unreliable output | §6.2 (C-02) | Side-by-side: single movement vs. stacked, same scene type, 10 generations each. If confirmed, promotes to L4. |
| V-02 | Camera verb synonym equivalence — whether `truck` and `lateral dolly` produce equivalent outputs | §4.5 (B-10) | Synonym substitution test, identical prompts. If confirmed, promotes vocabulary distinction to L4. |
| V-03 | Front-weighted element processing — whether earlier elements receive more weight | §6.2 (C-01) | Element position swap test with controlled content. If confirmed, promotes to L4. |
| V-04 | Whether Kelvin values in prompt text influence actual output color temperature | §6.3 | Side-by-side: character vocabulary vs. Kelvin value. If confirmed, promotes to L4. |
| V-05 | Ambient-only audio suppression — whether `Ambient noise:` declaration reliably suppresses music, speech, SFX | §5.3, §7.14 | Audio review of clips: compare `Ambient noise:` declaration vs. omission vs. various descriptor forms. If confirmed, promotes audio declaration effectiveness from L5 to L4. |
| V-06 | Whether an API-level audio suppression parameter exists separate from prompt text | §4.4 | MUAPI and Veo API documentation review. If confirmed, promotes to L1. |
| V-07 | Default vignette behavior — whether Veo consistently applies a vignette effect, and best suppression approach | §6.2 (C-07) | Generate identical prompts, review for vignette; test descriptor-form suppression. If confirmed, promotes to L4. |
| V-08 | Dutch angle suppression — whether `level camera horizon` descriptor reliably prevents canted output when channel prohibits it | §7.5 | Test with and without descriptor on scene types with dutch angle risk. If confirmed, promotes to L4. |
| V-09 | Whether `[4K]` or other quality tags in prompt text affect output quality vs. equivalent descriptor form | §4.5 | Compare tag vs. descriptor: `"4K"` vs. `"ultra-high definition"`. If confirmed, promotes to L4. |

---

## 13. Failure Modes

### FM-01 — Audio Default Generation

**Description:** Veo generates audio inferred from visual content as a default behavior. Audio is not suppressed by omission.

**Evidence:** VEO-DOC-01 [Level: L1 — audio generation is default; declaration is recommended]

**Impact:** Inferred audio (crowd noise, mechanical sounds, music) competes with pipeline's narration and licensed music tracks.

**Suppression:** Dedicated audio declaration sentence using `Ambient noise:` prefix with descriptor-form exclusions (§5.3, §7.14). [L2 for declaration syntax; L5 for suppression effectiveness — REQUIRES VERIFICATION: V-05]

---

### FM-02 — Centre-Frame Default

**Description:** Observation: primary subjects tend to appear centered in the frame for close-up and medium shots without explicit compositional instruction.

**Evidence:** VEO-HYP-02 [Level: L5 — no official documentation, no controlled experiment]

**Impact:** Centered compositions may conflict with §13.1 (right 40–60% default).

**Suppression:** Explicit positional language in prompt — [L5] for effectiveness.

---

### FM-03 — Sourceless Ambient Lighting

**Description:** Observation: without explicit source direction and entry language, Veo may default toward diffuse ambient illumination rather than the channel's directional character.

**Evidence:** VEO-HYP-02 [Level: L5]; consistent with L2 vocabulary — film noir and Rembrandt vocabulary in official docs implies directionality is achievable

**Impact:** Even illumination conflicts with §8.4 (no flat fill).

**Suppression:** Name a directional source character and direction (§7.2) — [L5] for mechanism.

---

### FM-04 — Anachronism Injection

**Description:** For period scenes, Veo may introduce modern elements from visual similarity to contemporary training data. Common: modern drainage, contemporary footwear, electric lighting.

**Evidence:** VEO-HYP-02 [Level: L5 — no official documentation of this behavior]

**Impact:** Period violations conflict with §17.

**Suppression:** Descriptor-form exclusion naming specific modern elements most likely for the scene type (§7.11). [L2 for format; L5 for suppression effectiveness]

---

### FM-05 — Frontal Face Generation

**Description:** Observation: when human figures are described or implied, Veo may default to frontal face presentation.

**Evidence:** VEO-HYP-02 [Level: L5]

**Impact:** Conflicts with §17.3.

**Suppression:** Descriptor approach: `figures seen from behind or in silhouette` (§7.11) — [L2] for format; [L5] for effectiveness.

---

### FM-06 — Prompt Truncation or Element Dropout

**Description:** With longer prompts approaching the 1,024 token limit, later elements may receive less attention.

**Evidence:** VEO-DOC-04 [Level: L1 — 1,024 token limit is confirmed]; attention dropout behavior is [L5]

**Impact:** Key late-prompt elements (audio declaration, negative descriptors) may not fully influence output.

**Suppression:** Place highest-priority elements early. [L5 for attention weighting hypothesis — V-03]

---

## 14. Capability Matrix

| Feature | Status | Notes |
|---|---|---|
| T2V (text-to-video) | SUPPORTED [L1: VEO-DOC-03] | Multiple model tiers |
| I2V (image-to-video) | SUPPORTED [L1: VEO-DOC-01] | Reference images up to 3; MUAPI uses images_list array |
| Duration: 4s | SUPPORTED [L1: VEO-DOC-02] | Not used in Hypogeum pipeline currently (configured to 8s) |
| Duration: 6s | SUPPORTED [L1: VEO-DOC-02] | Not used in Hypogeum pipeline currently |
| Duration: 8s | SUPPORTED [L1: VEO-DOC-02] | Current Hypogeum pipeline configuration |
| Aspect ratio: 16:9 | SUPPORTED [L1: VEO-DOC-03] | Primary production ratio |
| Aspect ratio: 9:16 | SUPPORTED [L1: VEO-DOC-03] | Shorts |
| Resolution: 720p | SUPPORTED [L1: VEO-DOC-03] | |
| Resolution: 1080p | SUPPORTED [L1: VEO-DOC-03] | |
| Resolution: 4K | SUPPORTED [L1: VEO-DOC-03] | Unavailable on Lite model |
| Audio generation | default-on [L1: VEO-DOC-01] | Inferred from visual content; declaration recommended |
| Timestamp multi-shot sequencing | SUPPORTED [L2: VEO-DOC-01] | `[00:00-00:02]` format is documented syntax; behavioral reliability is L5 |
| Reference image input | SUPPORTED [L1: VEO-DOC-01] | Up to 3 images; consistency mechanism is L5 |
| First + last frame interpolation | SUPPORTED [L1: VEO-DOC-01] | |
| Video extension | SUPPORTED [L1: VEO-DOC-01] | Up to 7s per extension, max 20 extensions |
| Dutch angle / canted horizon | SUPPORTED [L2: VEO-DOC-01] | Listed in official vocabulary; channel prohibits it (§11.3) — channel rule, not provider limitation |
| Camera movement vocabulary | DOCUMENTED [L2: VEO-DOC-01] | Terms appear in official docs; behavioral reliability when used in prompts is L5 |
| Bracketed `[no X]` negative constraints | NOT RECOMMENDED [L2: VEO-DOC-01] | Official guidance recommends against "no"/"don't" phrasing |
| API-level audio suppression parameter | REQUIRES VERIFICATION (V-06) | Not confirmed in available documentation |

---

## 15. Future Extensions

### 15.1 Priority Verification Work

1. **V-05** (audio suppression) — highest priority: §21.3 compliance cannot be confirmed until ambient-only suppression effectiveness is verified; would promote from L5 to L4
2. **V-07** (vignette suppression mechanism) — important for output quality consistency
3. **V-01** (stacked movement behavior) — needed to confirm or deny AP-01 as an architectural rule; would promote C-02 from L5 to L4
4. **V-08** (dutch angle suppression) — needed before CPA can safely handle §11.3 for Veo

### 15.2 L4 Promotion Path

No items in this skill currently qualify for L4 — VEO-HYP-02 has no documented experiment IDs, prompts, or controlled outputs. To promote any Section C item to L4, a formal experiment must be conducted with documented: (a) experiment ID, (b) prompt used, (c) output description, (d) statement of what the observation demonstrates. V-01 through V-09 are the priority test cases.

### 15.3 I2V-Specific Documentation

All behavioral claims in Sections 4–6 are derived from T2V observations. I2V behavior (when an anchor image is provided) may differ in subject composition defaults, lighting interpretation, and movement range. Pending I2V-specific documentation.

### 15.4 Negative Constraint Resolution

The official documentation recommends against `[no X]` format [L2]. Previous Hypogeum CPA conventions use this format throughout. Before V3 (Video Brand Guardian), the CPA must be audited and updated to use officially recommended descriptor-form exclusions. This is a prerequisite for §17 and §18 compliance.

### 15.5 Version History

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-07 | Initial creation as veo_prompt_skill.md (8 sections + Appendix A) |
| 1.0 | 2026-07-08 | Restructured as veo_translation_skill.md to conform to template v1.0 (Phase 0.8). 12 sections. |
| 2.0 | 2026-07-08 | Phase 0.9 evidence-based refactor. Evidence Registry added. Three-section structure (A/B/C). Rule Traceability added. Official Veo documentation sourced. Major corrections: duration updated to 4/6/8s; negative constraint format corrected to official descriptor approach; dutch tilt reclassified as channel prohibition not provider limitation; temporal format updated to official timestamp syntax. Most behavioral claims downgraded to HYPOTHESIS or REQUIRES VERIFICATION. |
| 3.0 | 2026-07-08 | Phase 1.0 evidence taxonomy refactor. Confidence + Status fields replaced with single Level field throughout. Evidence Registry Max Claim Level column added. Capability Matrix status values updated to L1/L2/L3. All B-items reclassified: capability facts (B-01, B-03, B-04, B-05, B-06, B-13) → L1; documented vocabulary and syntax (B-02, B-07, B-08, B-09, B-10, B-11, B-12, B-14) → L2. All Section C items → L5. Translation table inline markers updated to [L1]/[L2]/[L5]. Rule Traceability Table: Verification Status column → Level column. No items qualify as L3 or L4 for Veo. |
