# Seedance Translation Skill
## Translation Reference — Video Style System → Seedance Generation Language

```
PROVIDER: Seedance 2.0 / seedance-v2.0-t2v, seedance-v2.0-i2v (MUAPI path); seedance2, seedance2_fast, seedance2_mini (Runway API)
GENERATION_TYPE: T2V+I2V
SKILL_VERSION: 3.0
STYLE_SYSTEM_SYNCED: video_style_system.md v1.2
CREATED: 2026-07-08
LAST_UPDATED: 2026-07-08
HIERARCHY_POSITION: Video Style System → Seedance Translation Skill → CPA → VBG → Runtime
PARENT_DOCUMENT: hypogeum/brand/video_style_system.md
DOWNSTREAM_DOCUMENT: hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md
```

---

## 1. Purpose

This document teaches the pipeline how Seedance interprets prompt language. It does not define cinematic rules. It does not make creative decisions. Every cinematic decision belongs to `video_style_system.md`. This document exists solely to translate those canonical decisions into the vocabulary and structure that Seedance reliably processes.

This document's sole responsibility is translation — and only translation — of canonical intent into Seedance-compatible prompt language. Where Seedance's behavior differs from what the canonical style system assumes, those differences are documented explicitly. Behavioral parity with other providers is not assumed and is not documented here.

**What this document is:** A provider-specific translation reference for Seedance.

**What this document is not:**
- Not the canonical authority — `video_style_system.md` is the sole canonical source
- Not a runtime module — it executes nothing
- Not a validator — it checks nothing
- Not a prompt template — it produces no prompts
- Not the Cinematic Prompt Architecture — the CPA derives its layer structure from this skill
- Not a substitute for production testing — behaviors marked `[REQUIRES VERIFICATION]` must be confirmed before being treated as architectural rules

**The translation chain:**

```
Video Style System (what the output must be)
        ↓
Seedance Translation Skill (how Seedance processes language)
        ↓
Cinematic Prompt Architecture (the assembled prompt structure)
        ↓
Generated clip (the output)
```

**Traceability principle:** Every CPA convention applied to Seedance-bound prompts must trace its vocabulary and structural choices to a specific behavior documented in this skill with a supporting Evidence ID and Level. If a CPA convention cannot be traced here, it is unverified for Seedance.

---

## 2. Provider Profile

### 2a. Provider Identification

**Provider:** Seedance 2.0 (ByteDance / Jimeng AI)

**MUAPI path identifiers (Hypogeum production pipeline):**
- `seedance-v2.0-t2v` — T2V path
- `seedance-v2.0-i2v` — I2V path

**Runway API identifiers (official API path):**
- `seedance2` — full model (T2V; up to 4K, up to 15s)
- `seedance2_fast` — fast tier (480p–720p; up to 15s)
- `seedance2_mini` — mini tier (480p–720p; up to 15s)

**Generation type:** T2V+I2V (both text-to-video and image-to-video supported)

### 2b. Supported Production Parameters

**Durations:** 4–15 seconds (user-selectable) [Evidence: SEED-API-01, SEED-COMM-01, L1]

- **Hypogeum MUAPI configuration:** 5-second clips [Evidence: SEED-HYP-01, L1 — pipeline configuration fact] — this is a Hypogeum pipeline configuration choice, not a model constraint.
- **Model capability:** 4–15 seconds

**Aspect ratios (Runway API — `seedance2` full model):**
Wide range from 480p to 4K, including: 1280×720, 720×1280, 960×960, 1920×1080, 1080×1920, 3840×2160, and others [Evidence: SEED-API-01, L1]

**Aspect ratios (Runway API — `seedance2_fast`, `seedance2_mini`):**
480p and 720p only: 1280×720, 720×1280, 960×960, and related ratios [Evidence: SEED-API-01, L1]

**Audio generation behavior:** `default-on` — boolean parameter, defaults to `true`. Audio (sound effects and background music) is automatically generated alongside video. [Evidence: SEED-API-01, L1]

**I2V input format (MUAPI path):** `images_list` array [Evidence: SEED-HYP-01, L1 — configuration fact: VERIFIED: 2026-06-06]

**I2V input format (Runway API):** `promptImage` — single image or array, with optional `position: "first"` or `"last"` for keyframe mode. Keyframe mode and reference image mode cannot be mixed. [Evidence: SEED-API-01, L1]

**Text input limit (Runway API):** Up to 3,500 characters for `promptText` [Evidence: SEED-API-01, L1]

**Multi-modal inputs (Seedance 2.0 native capability):**
- Images: up to 9 per generation (jpeg, png, webp, bmp, tiff, gif; max 30 MB each)
- Videos: up to 3 (mp4, mov; max 50 MB each, total duration 2–15s)
- Audio: up to 3 (mp3, wav; max 15 MB each, total ≤ 15s)
- Total files: up to 12 combined
[Evidence: SEED-COMM-01, L3 — attributed to official ByteDance documentation by source]

### 2c. Known Quality Profile

- Natively supports multi-modal input including reference images, reference videos, and reference audio — the `@Reference` system is a defining capability [Evidence: SEED-COMM-01, L3]
- Audio generation is enabled by default with a boolean parameter; Seedance generates sound effects and background music inferred from the scene [Evidence: SEED-API-01, L1]
- Supports extended durations (4–15 seconds) and time-segmented multi-shot prompts [Evidence: SEED-API-01 (L1 for duration), SEED-COMM-01 (L3 for time-segmented format)]
- Wide aspect ratio support including 4K on the full model tier [Evidence: SEED-API-01, L1]
- Most fine-grained behavioral characteristics (lighting interpretation, material specificity response, atmospheric anchoring, camera movement fidelity) are NOT documented in available official or community sources and are currently L5
- The Hypogeum MUAPI path uses a 5-second configuration — this does not represent the model's capability limit

### 2d. Behavior Summary

Seedance 2.0 is a unified multimodal audio-video generation model that accepts text, image, video, and audio references as input, and produces audio alongside video by default. Its defining capability is the `@Reference` system, which allows explicit role assignment to input assets (character appearance, camera movement, audio style, etc.). Available community documentation describes a structured prompt formula and standard film camera vocabulary. The model's fine-grained prompt response characteristics — how it interprets specific lighting descriptions, material specificity, atmospheric anchoring — are not formally documented and must be treated as L5 until systematically verified through documented Hypogeum experiments qualifying for L4. The Hypogeum MUAPI path currently configures 5-second clips, but the model supports 4–15 seconds.

---

## 3. Evidence Registry

| ID | Source Type | Name | URL | Version | Date Accessed | Max Claim Level |
|---|---|---|---|---|---|---|
| SEED-API-01 | OFFICIAL_REPO | Runway API specification for seedance2 (runwayml/skills) | https://github.com/runwayml/skills | Current | 2026-07-08 | L1, L2 — official Runway API schema |
| SEED-COMM-01 | COMMUNITY_SKILL | dexhunter/seedance2-skill SKILL.md | https://github.com/dexhunter/seedance2-skill | Current | 2026-07-08 | L3 — attributed to "official ByteDance documentation" but not independently verifiable |
| SEED-COMM-02 | COMMUNITY_SKILL | Square-Zero-Labs/video-prompting-skill (seedance2 model reference) | https://github.com/Square-Zero-Labs/video-prompting-skill | Current | 2026-07-08 | Section B use only; not evidence of Seedance-specific behavior |
| SEED-HYP-01 | HYPOGEUM_EXP | Hypogeum MUAPI benchmark test (2026-06-06) | Internal | — | 2026-06-06 | L1 for configuration facts (duration used, I2V format, latency measurement); L5 for behavioral claims — no formal experiment ID |
| SEED-HYP-02 | HYPOGEUM_EXP | Hypogeum MUAPI implementation (byteplus_ark.py, config.py) | Internal | — | 2026-07-08 | L1 for API format facts |

---

## 4. Verified Provider Behaviour (Section A)

**Scope:** Facts about what Seedance does — API parameters, capability facts, officially documented behaviors and syntax. Sources qualifying for L1 or L2 preferred; L3 items appear here only when the community skill explicitly attributes them to official documentation.

### 4.1 API Parameters and Generation Defaults

**B-01 — Audio Default**

The Runway API for `seedance2` includes an `audio` boolean parameter. Default value: `true`. When enabled, Seedance generates sound effects and background music alongside the video.

- Evidence: SEED-API-01
- Level: L1
- §Ref: §21 Audio Policy

**B-02 — Text Prompt Length**

Maximum prompt text length: 3,500 characters for `promptText` via Runway API.

- Evidence: SEED-API-01
- Level: L1
- §Ref: —

**B-03 — I2V Format (MUAPI path)**

Seedance image-to-video via the Hypogeum MUAPI path uses the `images_list` array format.

- Evidence: SEED-HYP-01
- Level: L1 (configuration fact)
- §Ref: —

**B-04 — I2V Format (Runway API)**

The Runway API uses `promptImage` — a single image or array — with optional `position: "first"` or `"last"` for keyframe/interpolation mode. Keyframe mode and reference image mode cannot be mixed in a single request.

- Evidence: SEED-API-01
- Level: L1
- §Ref: —

### 4.2 Supported Durations

**B-05 — Duration Range**

Seedance 2.0 supports durations of 4–15 seconds (user-selectable).

- Evidence: SEED-API-01 (Runway API spec: Min: 4, Max: 15), SEED-COMM-01 (SKILL.md: "4–15 seconds user-selectable")
- Level: L1 (SEED-API-01 is authoritative; SEED-COMM-01 corroborates)
- §Ref: §23.1

**Critical correction from previous skill version:** Previous versions of this skill documented 5-second duration as "fixed" for Seedance. This was incorrect — it reflected the Hypogeum MUAPI pipeline configuration (Section C, C-01), not the model's capability. The model supports 4–15 seconds.

### 4.3 Supported Aspect Ratios

**B-06 — Aspect Ratio Range (`seedance2` full model via Runway API)**

The full `seedance2` model supports aspect ratios from 480p to 4K, including: 1280×720 (16:9), 720×1280 (9:16), 960×960 (1:1), 1920×1080 (16:9 1080p), 1080×1920 (9:16 1080p), 3840×2160 (4K), and numerous others.

- Evidence: SEED-API-01
- Level: L1
- §Ref: —

**B-07 — Aspect Ratio Range (`seedance2_fast`, `seedance2_mini` via Runway API)**

Fast and mini tiers are limited to 480p and 720p ratios only: 1280×720, 720×1280, 960×960, and related ratios.

- Evidence: SEED-API-01
- Level: L1
- §Ref: —

### 4.4 Audio Behavior

**B-08 — Audio Generation Default**

Seedance generates audio (sound effects, background music) by default as part of video output. This is controlled by the `audio` boolean parameter (default: `true`).

- Evidence: SEED-API-01
- Level: L1
- §Ref: §21.1, §21.2

**B-09 — Audio Types Supported**

Community skill documentation attributes these audio capabilities to Seedance 2.0: auto-generated sound effects and background music. Reference audio input is also supported — `@Audio1` reference allows controlling audio character.

- Evidence: SEED-COMM-01 (attributed to official ByteDance documentation)
- Level: L3 (community recommendation; not independently verified against official source)
- §Ref: §21

**B-10 — Audio Suppression Parameter**

The Runway API's `audio` boolean parameter set to `false` suppresses audio generation. This is a generation parameter, not a prompt expression.

- Evidence: SEED-API-01
- Level: L1 (API parameter exists and is documented)
- §Ref: §21.3

**Note on §21.3 compliance:** Setting `audio: false` via the API parameter is the clearest mechanism for achieving the channel's audio-free requirement. Whether this parameter is accessible via the Hypogeum MUAPI path requires confirmation [REQUIRES VERIFICATION: V-01].

### 4.5 Officially Supported Camera Vocabulary

**B-11 — Camera Movement Terms (Community Skill)**

The following camera terms are documented in the Seedance 2.0 community skill, attributed to official ByteDance documentation:

**Basic movements:**

| Term | Description |
|---|---|
| Push in / Slow push | Camera moves toward subject |
| Pull back / Pull away | Camera moves away from subject |
| Pan left/right | Horizontal rotation |
| Tilt up/down | Vertical rotation |
| Track / Follow shot | Camera follows moving subject |
| Orbit / Revolve | Camera circles around subject |

**Advanced techniques:**

| Term | Description |
|---|---|
| Hitchcock zoom (dolly zoom) | Push in + zoom out (or vice versa) — vertigo effect |
| Fisheye lens | Ultra-wide distorted lens |
| Low angle / High angle | Camera below/above subject |
| Bird's eye / Overhead | Top-down |
| First-person POV | Subjective camera |
| Whip pan | Very fast horizontal pan with motion blur |
| Crane shot | Vertical movement like crane arm |

**Shot sizes:**

| Term | Description |
|---|---|
| Extreme close-up | Eyes, mouth, or small detail only |
| Close-up | Face fills frame |
| Medium close-up | Head and shoulders |
| Medium shot | Waist up |
| Full shot | Entire body |
| Wide / Establishing shot | Full environment |

- Evidence: SEED-COMM-01
- Level: L3 (community recommendation — these terms appear in the community skill attributed to official ByteDance docs; behavioral reliability when used in prompts is L5 pending L4 testing)
- §Ref: §11, §12

### 4.6 Officially Supported Prompt Syntax

**B-12 — @Reference System**

Seedance 2.0's core feature is the `@Reference` syntax for assigning roles to uploaded assets. Each uploaded asset must have an explicit role declaration:

| Purpose | Syntax |
|---|---|
| First frame | `@Image1 as the first frame` |
| Last frame | `@Image2 as the last frame` |
| Character appearance | `@Image1's character as the subject` |
| Scene/background | `scene references @Image3` |
| Camera movement | `reference @Video1's camera movement` |
| Action/motion | `reference @Video1's action choreography` |
| BGM style | `BGM references @Audio1` |
| Sound effects | `sound effects reference @Video3's audio` |
| Outfit/clothing | `wearing the outfit from @Image2` |

- Evidence: SEED-COMM-01
- Level: L3 (community documentation — this syntax is documented in the community skill attributed to official ByteDance docs; whether all role types are reliably honored is L5)
- §Ref: §19 (motion reference), §34 (character consistency)

**B-13 — Time-Segmented Prompt Format**

For 10-second and longer videos, time-segmented prompts are documented:

```
0–3s: [opening scene description, camera, action]
3–6s: [mid-section development]
6–10s: [climax or key action]
10–15s: [resolution, ending shot]
```

- Evidence: SEED-COMM-01
- Level: L3 (community documentation — behavioral fidelity to segment boundaries is L5 pending L4 testing)
- §Ref: §19.2

### 4.7 Negative Constraint Format

**B-14 — Negative Constraint Documentation**

No official documentation or community skill for Seedance 2.0 specifies a recommended negative constraint format. The community skill focuses on positive description and reference-based generation; it explicitly identifies "vague references" and "conflicting instructions" as mistakes to avoid, but does not document a negative constraint syntax.

- Evidence: SEED-COMM-01 (explicitly documents what NOT to do, but not a negative constraint syntax)
- Level: L3 for the community skill's approach; the absence of a documented format is [NOT OFFICIALLY DOCUMENTED]
- §Ref: §17, §18

**Implication:** Negative constraints for Seedance must currently be treated as L5. The Hypogeum CPA should use positive-description approaches (describe what the scene IS, not what it should lack) until a reliable negative constraint mechanism is verified.

---

## 5. Prompt Authoring Guidance (Section B)

**Scope:** Provider-endorsed techniques for writing effective Seedance prompts. Sources: SEED-COMM-01 (L3), SEED-COMM-02 (Section B use only). Generic video prompting repositories are permitted here as general technique guidance.

### 5.1 Recommended Prompt Structure

The Seedance 2.0 community skill documents this 8-part prompt structure:

```
[Subject/Character Setup] + [Scene/Environment] + [Action/Motion Description] +
[Camera Movement] + [Timing Breakdown] + [Transitions/Effects] +
[Audio/Sound Design] + [Style/Mood]
```

- Evidence: SEED-COMM-01
- Level: L3

**For 10s+ videos:** Use time-segmented format:
```
0–3s: [opening scene description, camera, action]
3–6s: [mid-section development]
6–10s: [climax or key action]
10–15s: [resolution, ending shot, final branding if applicable]
```

### 5.2 Camera Language Authoring

Use the film vocabulary from §4.5 (B-11). State camera movements explicitly using cinematic terminology.

**Example from community skill:**
> "Completely reference @Video1's camera movements and protagonist's facial expressions. Hitchcock zoom during fear moment, then orbit shots..."

- Evidence: SEED-COMM-01
- Level: L3

### 5.3 Audio Declaration Authoring

Include explicit audio direction in every prompt. The community skill identifies ignoring audio as a common mistake.

**For channel production (ambient-only intent):**

Since the `audio` boolean parameter can suppress audio at the API level [B-10, L1: SEED-API-01], the primary suppression mechanism is the API parameter, not the prompt text.

- Set `audio: false` in the API request to suppress generated audio [L1: SEED-API-01 — parameter exists]
- Confirm this parameter is accessible on the Hypogeum MUAPI path [REQUIRES VERIFICATION: V-01]
- If not accessible via MUAPI, use prompt-based declaration: describe desired audio as ambient-only using the `[Audio/Sound Design]` element of the 8-part formula: `Sound design: natural environmental ambience only, no music, no speech` [L5 for this fallback's effectiveness]

- Evidence: SEED-API-01 [L1 for parameter], SEED-COMM-01 [L3 for audio as prompt element]

### 5.4 Negative Constraint Authoring

No documented negative constraint format exists for Seedance 2.0 [B-14]. Use positive description approaches:

- Describe what the scene IS, not what it should lack
- For period accuracy: describe the period elements explicitly (`ancient stone construction, practical torch lighting`)
- For figure framing: describe the desired framing (`figure seen from behind, silhouetted against the light`)
- Avoid conflicting instructions — the community skill explicitly identifies these as a failure mode

- Evidence: SEED-COMM-01 [L3 — positive description emphasis; conflicting instructions as failure mode]

### 5.5 @Reference System Authoring

When assets are uploaded, every asset must have an explicit role declaration using the `@` syntax [B-12].

**Failure mode to avoid (from community skill):** "Vague references — don't say 'reference @Video1' without specifying WHAT to reference." Always append the specific reference type: `@Video1's camera movement`, `@Video1's action choreography`, `@Image1's character`, etc.

- Evidence: SEED-COMM-01 [L3]

### 5.6 Physical Description Guidance

General video prompting guidance recommends describing motion in terms of "visible physical outcomes" — balance shifts, fabric movement, impact effects — rather than abstract mood language.

- Evidence: SEED-COMM-02 [general guidance — not Seedance-specific evidence]
- Level: L3 for general guidance; NOT evidence of how Seedance specifically responds to physical vs. mood language

---

## 6. Hypogeum Production Experience (Section C)

**Scope:** Observations and hypotheses from Hypogeum's own production use of Seedance. All items carry reduced authority. All behavioral claims are L5 (hypothesis) unless they represent a formally documented experiment qualifying for L4. No L4 items exist in this section — SEED-HYP-01 has no documented experiment IDs, prompts, or controlled behavioral outputs.

### 6.1 Configuration Notes

**C-01 — Hypogeum MUAPI 5-Second Configuration**

The Hypogeum pipeline routes Seedance via MUAPI and is currently configured for 5-second clip generation. This is a pipeline configuration choice, not the model's duration limit (which is 4–15s per SEED-API-01). The 5-second configuration was adopted for cost management.

- Evidence: SEED-HYP-01
- Level: L1 for configuration fact (the pipeline IS configured to 5s)
- §Ref: §23.1

**C-02 — Generation Latency (MUAPI path)**

Observed latency from Hypogeum benchmark (2026-06-06):
- T2V: approximately 103 seconds per generation
- I2V: approximately 96 seconds per generation

This is a measured pipeline fact, not a behavioral claim. Note: latency varies with load.

- Evidence: SEED-HYP-01
- Level: L1 for measurement fact (no experiment ID required for latency measurement)
- §Ref: —

**C-03 — MUAPI Audio Parameter Accessibility**

Whether the `audio` boolean parameter (documented in Runway API) is accessible via the Hypogeum MUAPI path has not been confirmed.

- Evidence: SEED-HYP-01 (absence of confirmation)
- Level: L5 — absence is not evidence; testing required
- §Ref: §21.3

### 6.2 Observations from Production Testing

**C-04 — Diagnostic Prompt Used in Benchmark**

The following prompt was used in the 2026-06-06 benchmark test:
> "A slow cinematic shot of an ancient bronze astronomical device on a dark museum table, no people, no text, no dialogue."

No behavioral conclusions have been drawn from this test beyond latency and configuration verification.

- Evidence: SEED-HYP-01
- Level: L1 for configuration and latency facts only; no behavioral claims

### 6.3 Hypotheses Under Investigation

The following behaviors are L5 — hypothesized based on general prompting knowledge and experience but not tested for Seedance. To qualify for L4, a formal experiment with documented ID, prompt, output, and observation statement is required.

| Hypothesis | Level | Priority |
|---|---|---|
| Physical description produces more specific output than mood language | L5 | HIGH — needed for §8, §10, §16 translations |
| Single camera movement produces more consistent output than stacked | L5 | HIGH — needed for §11 translation (note: stacking is L3-documented failure per SEED-COMM-01; behavioral effect confirmation would be L4) |
| Material specificity with surface condition and aging state improves output | L5 | HIGH — needed for §16 translation |
| Atmospheric elements anchored to light source produce spatial specificity | L5 | MEDIUM — needed for §10 translation |
| Front-weighted element ordering influences output | L5 | MEDIUM — needed for §5.1 guidance |
| Negative constraints (any format) reliably suppress described elements | L5 | HIGH — needed for §17, §18 compliance |

---

## 7. Translation Rules

Maps each canonical concept from `video_style_system.md` to its expression in Seedance prompt language. Evidence basis is cited for each translation approach with level; approaches derived from Hypogeum hypothesis are marked [L5].

**Important caveat:** The majority of translation rules in this section are L5 for Seedance. The community skill (SEED-COMM-01) describes prompt structure and camera vocabulary (L3) but does not document how Seedance responds to specific types of content description (lighting, materials, atmosphere). These are extrapolated from general video prompting knowledge and must be verified through documented experiments (L4) before being treated as architectural rules.

### 7.1 Color System (§7)

**Canonical:** §7 defines the channel identity palette.

**Evidence basis:** No Seedance-specific documentation on color expression. Extrapolated from general prompting approach [L5].

| Canonical Concept | Seedance Prompt Expression | Level |
|---|---|---|
| Color temperature | Describe through light source character | [L5] |
| Dark shadow zones | Describe shadow areas positively: `deep shadow zones with directional falloff` | [L5] |
| Warm amber light character | `warm amber light`, `torch-warm illumination` | [L5] |

### 7.2 Lighting System (§8)

**Canonical:** §8 requires a warm key light (motivated, directional, period-plausible) and subordinate secondary source.

**Evidence basis:** L5 for all specific lighting behaviors. The community skill uses broad style descriptors ("Tense and suspenseful", "Warm and healing") suggesting mood-register vocabulary may be relevant, but how Seedance responds to physical-source descriptions is not documented.

| Canonical Requirement | Seedance Prompt Expression | Level |
|---|---|---|
| Warm key light with direction | `warm amber light from the upper left, raking across the surface` | [L5] |
| Sharp falloff | `directional falloff into shadow` | [L5] |
| Secondary subordinate source | `cooler secondary light providing edge definition` | [L5] |
| No flat fill (§8.4) | Positive: `strong directional contrast` | [L5] |

### 7.3 Contrast and Shadow (§9)

**Canonical:** §9 requires high directional contrast, true near-black shadows.

**Evidence basis:** L5 — no Seedance-specific documentation.

| Canonical Requirement | Seedance Prompt Expression | Level |
|---|---|---|
| Near-black shadows | `deep shadow zones` | [L5] |
| Warm amber highlights | `warm amber highlight at the surface peak` | [L5] |

### 7.4 Atmosphere (§10)

**Canonical:** §10 requires atmospheric evidence of physical inhabitation.

**Evidence basis:** L5 — no Seedance-specific documentation.

| Canonical Type | Seedance Prompt Expression | Level |
|---|---|---|
| Underground particulate | `stone dust in the light beam` | [L5] |
| Combustion haze | `torch smoke drifting upward at the vault ceiling` | [L5] |
| Moisture | `moisture condensation on the stone face` | [L5] |

### 7.5 Camera Language (§11)

**Canonical:** §11 requires single motivated camera movement per scene (§11.2); prohibits dutch tilt for this channel (§11.3).

**Evidence basis:** Camera vocabulary is L3 [B-11: SEED-COMM-01]. The community skill explicitly identifies conflicting camera instructions as a failure mode (L3). That the model reliably executes these as cinematic movements in practice is L5 pending L4 testing.

| Canonical Function | Seedance Prompt Expression | Level |
|---|---|---|
| Discovering a subject | `slow push in` | [L3] (term documented in community skill) |
| Withdrawing | `pull back` | [L3] (term documented) |
| Surveying | `pan left` / `pan right` | [L3] (term documented) |
| Descending | `crane shot descending` | [L3] (term documented) |
| Encircling | `orbit shot around the subject` | [L3] (term documented) |
| Establishing hold | `static camera` | [L3] (term documented) |
| POV exploration | `first-person POV` | [L3] (term documented) |

**Level note for camera vocabulary:** [L3] means the term appears in the community skill attributed to official ByteDance docs. Whether the model reliably executes these as the intended cinematic movement requires L4 evidence (documented Hypogeum experiment).

**Stacking movements:** The community skill explicitly lists "conflicting instructions — don't ask for 'static camera' AND 'orbit shot' in same segment" as a mistake [SEED-COMM-01, L3]. This confirms the community recommends against stacking. Whether stacking reliably produces degraded output is L5 pending L4 testing.

### 7.6 Lens Language and Shot Scale (§12)

**Canonical:** §12 defines nine permitted shot scales.

**Evidence basis:** Shot size vocabulary is L3 [B-11: SEED-COMM-01]. Behavioral reliability is L5.

| Canonical Shot Scale | Seedance Prompt Term | Level |
|---|---|---|
| Extreme close-up | `extreme close-up` | [L3] (term documented) |
| Close-up | `close-up` | [L3] (term documented) |
| Medium close-up | `medium close-up` | [L3] (term documented) |
| Medium | `medium shot` | [L3] (term documented) |
| Full shot | `full shot` | [L3] (term documented) |
| Wide establishing | `wide shot` / `establishing shot` | [L3] (term documented) |
| Overhead | `bird's eye view` / `overhead` | [L3] (term documented) |
| Low angle | `low angle` | [L3] (term documented) |

### 7.7 Composition and Spatial Layering (§13)

**Canonical:** §13 requires primary subject in right 40–60% of frame, minimum two spatial layers.

**Evidence basis:** [NOT OFFICIALLY DOCUMENTED] — no Seedance-specific documentation on compositional positioning. L5.

| Canonical Requirement | Seedance Prompt Expression | Level |
|---|---|---|
| Subject positioning | `subject positioned in the right half of the frame` | [L5] |
| Spatial layers | Describe both layers as physical elements at different distances | [L5] |

### 7.8 Subject Scale (§14)

**Canonical:** §14 defines monumental and scale-contrast approaches.

**Evidence basis:** Shot scale vocabulary is L3 [B-11: SEED-COMM-01]. Frame percentage control is L5.

| Canonical Approach | Seedance Prompt Expression | Level |
|---|---|---|
| Monumental | Close-up + `subject fills the vertical frame` | [L5] for scale control; [L3] for shot scale vocabulary |
| Scale contrast | Wide establishing shot + `small figure dwarfed by the environment` | [L5] for scale control; [L3] for shot scale vocabulary |

### 7.9 Environmental Storytelling (§15)

**Canonical:** §15 prohibits void backgrounds.

**Evidence basis:** L5 — no Seedance-specific documentation.

| Canonical Requirement | Seedance Prompt Expression | Level |
|---|---|---|
| No void backgrounds | Name at least one physical environmental element in background | [L5] |
| Spatial evidence | `worn stone at the threshold` / `soot above the torch bracket` | [L5] |

### 7.10 Material Authenticity (§16)

**Canonical:** §16 requires period-specific material observations with age and use history.

**Evidence basis:** L5 — the community skill uses broad style descriptors but does not document how Seedance responds to period-specific material detail.

| Period Material | Seedance Prompt Expression | Level |
|---|---|---|
| Travertine | `travertine surface — raking light on raised face, shadow in recessed joints` | [L5] |
| Roman concrete | `opus incertum concrete — irregular aggregate in thick mortar` | [L5] |
| Age indicators | `mineral staining, soot blackening, root penetration` | [L5] |

### 7.11 Historical Authenticity — Anachronism Suppression (§17)

**Canonical:** §17 prohibits modern elements. §17.3 prohibits frontal faces.

**Evidence basis:** No negative constraint format documented for Seedance [B-14, NOT OFFICIALLY DOCUMENTED]. Use positive description approach [L3 for positive description emphasis from SEED-COMM-01; L5 for suppression effectiveness].

| Channel Requirement | Seedance Prompt Approach | Level |
|---|---|---|
| No modern elements | Positive: `ancient stone construction, period-accurate materials, practical torch light` | [L3] for positive-description approach; [L5] for suppression effectiveness |
| No frontal faces (§17.3) | Positive: `figures seen from behind, silhouetted against the light` | [L5] |

### 7.12 Realism — CGI and Illustration Suppression (§18)

**Canonical:** §18 prohibits CGI appearance, illustration, game-engine register.

**Evidence basis:** The community skill uses `cinematic quality, film grain, shallow depth of field` as vocabulary [SEED-COMM-01, L3] for realistic output. Whether these suppress CGI register is L5.

| Channel Requirement | Seedance Prompt Expression | Level |
|---|---|---|
| Photorealistic register | `cinematic quality, photorealistic` | [L3] for vocabulary; [L5] for CGI suppression effectiveness |
| No CGI register | Positive material specificity (period materials with surface detail) | [L5] |

### 7.13 Motion and Temporal Structure (§19)

**Canonical:** §19 requires physical change within each clip, at least one secondary motion element.

**Evidence basis:** Time-segmented format is L3 [B-13: SEED-COMM-01]. Behavioral fidelity to segment boundaries is L5.

| Canonical Requirement | Seedance Prompt Expression | Level |
|---|---|---|
| Temporal structure (5s MUAPI config) | `0-2.5s: [opening state]. 2.5-5s: [physical change].` | [L5] for phase split; [L3] for time-segmented format concept |
| Temporal structure (8s channel standard) | `0-4s: [opening state]. 4-8s: [physical change].` | [L5] for phase split; [L3] for time-segmented format concept |
| Secondary motion element | Name alongside camera movement in the prompt | [L5] |

**Duration note:** The Hypogeum pipeline uses 5-second clips (C-01). If the pipeline is reconfigured to 8 seconds, the temporal structure adapts accordingly. The community skill documents time-segmented format for up to 15s [B-13, L3].

### 7.14 Audio Policy (§21)

**Canonical:** §21 requires explicit audio intent declaration; natural environmental ambience only; no music, speech, narration.

**Evidence basis:** Audio boolean parameter is L1 [B-10: SEED-API-01]. API-level suppression is the primary recommended mechanism. Prompt-based fallback is L5.

| Canonical Audio Requirement | Seedance Mechanism | Level |
|---|---|---|
| Audio suppression | Set `audio: false` in API request | [L1] (parameter exists: SEED-API-01) |
| MUAPI path suppression | [REQUIRES VERIFICATION: V-01] — unknown whether MUAPI exposes this parameter | REQUIRES VERIFICATION |
| Prompt-based declaration (fallback) | Include audio design element: `Sound design: ambient environmental sounds only, no music, no speech` | [L5] for effectiveness |

**Priority:** Confirm V-01 (whether MUAPI path exposes the `audio` boolean) before V3 (Video Brand Guardian). If not exposed, a pipeline-level workaround must be designed.

### 7.15 Scene Narrative Focus (§33)

**Canonical:** §33 requires one primary narrative focus and one primary visual action per scene.

**Evidence basis:** The community skill's structured formula implies one subject + one action per time segment [SEED-COMM-01, L3]. Fine-grained Seedance behavioral fidelity to this structure is L5.

| Canonical Rule | Seedance Prompt Expression | Level |
|---|---|---|
| One primary subject | Name primary subject once in character/subject setup element | [L3] for formula structure; [L5] for behavioral fidelity |
| One primary visual action | One action descriptor per time segment | [L3] for formula structure; [L5] for behavioral fidelity |

### 7.16 Cross-Episode Visual Continuity (§34)

**Canonical:** §34 requires consistent color temperature, atmosphere, and lighting character across same-setting clips.

**Evidence basis:** The `@Reference` system [B-12, L3: SEED-COMM-01] enables character and style consistency via reference images — this is a documented Seedance mechanism. Whether it reliably produces the intended consistency is L5 pending L4 testing.

| Canonical Requirement | Seedance Mechanism | Level |
|---|---|---|
| Consistent character appearance | `@Image1's character as the subject` across all same-character clips | [L3] for @Reference syntax; [L5] for consistency effectiveness |
| Consistent visual style | `scene references @Image3` for consistent background/environment | [L3] for @Reference syntax; [L5] for consistency effectiveness |
| Consistent vocabulary anchors | Reuse identical vocabulary across same-setting prompts | [L5] (no-memory independent generations) |

---

## 8. Rule Traceability Table

| Rule (§7 subsection) | Evidence IDs | §Ref | Level |
|---|---|---|---|
| 7.1 Color System | SEED-HYP-01 | §7 | L5 |
| 7.2 Lighting System | SEED-HYP-01 | §8 | L5 |
| 7.3 Contrast and Shadow | SEED-HYP-01 | §9 | L5 |
| 7.4 Atmosphere | SEED-HYP-01 | §10 | L5 |
| 7.5 Camera Language | SEED-COMM-01 (vocabulary L3; no-stacking L3) | §11 | L3 for vocabulary and anti-stacking; L5 for behavioral reliability |
| 7.6 Lens Language and Shot Scale | SEED-COMM-01 | §12 | L3 for shot scale vocabulary; L5 for behavioral reliability |
| 7.7 Composition and Spatial Layering | None | §13 | L5 |
| 7.8 Subject Scale | SEED-COMM-01 (shot scale L3), SEED-HYP-01 | §14 | L3 for shot scale vocabulary; L5 for frame percentage control |
| 7.9 Environmental Storytelling | None | §15 | L5 |
| 7.10 Material Authenticity | None | §16 | L5 |
| 7.11 Anachronism Suppression | SEED-COMM-01 (positive description L3) | §17 | L3 for positive-description approach; L5 for suppression effectiveness |
| 7.12 CGI Suppression | SEED-COMM-01 (cinematic quality vocabulary L3) | §18 | L3 for vocabulary; L5 for CGI suppression effectiveness |
| 7.13 Motion and Temporal Structure | SEED-COMM-01 (time-segmented format L3) | §19 | L3 for format; L5 for phase split behavioral fidelity |
| 7.14 Audio Policy | SEED-API-01 (audio boolean L1); SEED-HYP-01 (MUAPI path: REQUIRES VERIFICATION) | §21 | L1 for Runway API parameter; REQUIRES VERIFICATION for MUAPI path |
| 7.15 Scene Narrative Focus | SEED-COMM-01 (formula structure L3) | §33 | L3 for one-subject-per-segment structure; L5 for behavioral fidelity |
| 7.16 Cross-Episode Continuity | SEED-COMM-01 (@Reference syntax L3) | §34 | L3 for @Reference syntax; L5 for consistency effectiveness |

---

## 9. Prompt Anti-Patterns

### AP-01 — Stacked Camera Movements

**Pattern:** `static camera` AND `orbit shot` in the same time segment. Any two concurrent movement instructions.

**Symptom:** The community skill explicitly identifies this as a mistake: "Conflicting instructions — don't ask for 'static camera' AND 'orbit shot' in same segment."

**Fix:** One camera movement term per segment (§11.2).

**Evidence:** SEED-COMM-01 [Level: L3 — community documentation of this failure]

---

### AP-02 — Contradictory Adjectives

**Pattern:** `dynamic but subtle`, `warm but cool`, `epic but intimate`.

**Symptom:** Unclear direction for the model; output likely to satisfy neither.

**Fix:** Commit to one direction; express nuance as two physically distinct elements.

**Evidence:** SEED-HYP-01 [Level: L5]

---

### AP-03 — Mood Vocabulary Without Physical Anchor

**Pattern:** `mysterious and ancient`, `dark and foreboding`, without physical specificity.

**Symptom:** Generic output.

**Fix:** Replace mood adjectives with physical observations.

**Evidence:** SEED-HYP-01 [Level: L5]; SEED-COMM-02 (general guidance, not Seedance-specific)

---

### AP-04 — Generic Material Description

**Pattern:** `stone`, `metal`, `wood` without surface condition or aging state.

**Symptom:** Smooth, generic surfaces without period specificity.

**Fix:** Include surface condition, aging state, and light behavior for every named material.

**Evidence:** SEED-HYP-01 [Level: L5]

---

### AP-05 — Audio Intent Omission

**Pattern:** No `audio: false` parameter set and no audio design element in prompt, when scene implies audio (crowds, mechanisms, fire, music).

**Symptom:** Seedance generates sound effects and background music by default, competing with pipeline audio tracks.

**Fix:** Set `audio: false` via API parameter if accessible. If not, include explicit audio design element in prompt (§7.14).

**Evidence:** SEED-API-01 [Level: L1 — audio is default-on; this is a capability fact]

---

### AP-06 — Vague @Reference Assignment

**Pattern:** `reference @Video1` without specifying what to reference.

**Symptom:** The community skill explicitly identifies this: "Vague references — don't say 'reference @Video1' without specifying WHAT to reference."

**Fix:** Always specify the reference type: `reference @Video1's camera movement`, `@Image1's character as the subject`, etc.

**Evidence:** SEED-COMM-01 [Level: L3 — community documentation of this failure]

---

### AP-07 — Overloading Short Time Segments

**Pattern:** Packing too many actions, transitions, or elements into a 4–5 second segment.

**Symptom:** The community skill identifies "overloading — don't pack too many scenes into 4–5 seconds" as a mistake.

**Fix:** Keep prompt complexity proportional to clip length. For 5-second Hypogeum clips, use one clear action per clip.

**Evidence:** SEED-COMM-01 [Level: L3 — community documentation of this failure]

---

## 10. Provider Mapping Table

| §Ref | Rule | Prompt Requirement | Explicit Expression Required |
|---|---|---|---|
| §7.1 | Color palette | IMPLICIT | Colors through light and material descriptions [L5] |
| §7.2 | Warm/cool balance | SEMI-EXPLICIT | Through directional lighting description [L5] |
| §8.1 | Two light sources | EXPLICIT | Describe both sources [L5] |
| §8.2 | Key light character | EXPLICIT | Directional light with source character [L5] |
| §8.3 | Secondary source | EXPLICIT | Named secondary source [L5] |
| §8.4 | No flat fill | EXPLICIT | Positive contrast description [L5] |
| §9.1 | Shadow blacks | EXPLICIT | Deep shadow description [L5] |
| §10.1 | Atmospheric evidence | EXPLICIT | Named atmospheric phenomenon [L5] |
| §11.1 | Camera height | EXPLICIT | Camera angle vocabulary [L3] |
| §11.2 | Motivated movement | EXPLICIT | One camera movement term per segment [L3 vocabulary; L5 reliability] |
| §11.3 | No dutch tilt (channel rule) | EXPLICIT | Positive: `level camera orientation` [L5] |
| §11.4 | Maximum hold | PLANNING | Scene planning stage |
| §12.1 | Shot scale | EXPLICIT | Shot size vocabulary [L3] |
| §12.2 | Depth of field | EXPLICIT | `shallow depth of field` / `cinematic quality` [L5] |
| §13.1 | Subject placement | EXPLICIT | Positional language [L5] |
| §13.3 | Spatial layers | EXPLICIT | Physical elements at different distances [L5] |
| §14.1 | Scale approach | EXPLICIT | Shot scale [L3] + subject size [L5] |
| §14.2 | Scale anchor | EXPLICIT | Named scale reference [L5] |
| §15.1 | No void backgrounds | EXPLICIT | Physical environment elements [L5] |
| §16.1 | Material specificity | EXPLICIT | Period material with surface detail [L5] |
| §16.2 | Material age | EXPLICIT | Aging indicators [L5] |
| §17.1 | Anachronism prohibition | EXPLICIT | Positive period description [L3 approach; L5 effectiveness] |
| §17.3 | No frontal faces | EXPLICIT | Descriptor: `figures from behind` [L5] |
| §18.1–§18.3 | No CGI/illustration | IMPLICIT + EXPLICIT | `photorealistic, cinematic quality` [L3 vocabulary] + material specificity [L5] |
| §19.2 | Temporal structure | EXPLICIT | Time-segmented format [L3]; segment fidelity [L5] |
| §19.3 | Secondary motion | EXPLICIT | Named alongside camera movement [L5] |
| §21.1 | Permitted audio | GENERATION PARAMETER | `audio: false` API parameter [L1: SEED-API-01] |
| §21.2 | Prohibited audio | GENERATION PARAMETER | `audio: false` API parameter [L1: SEED-API-01] |
| §21.3 | Audio intent declaration | GENERATION PARAMETER | Set at API level [L1]; MUAPI path: REQUIRES VERIFICATION (V-01) |
| §23.1 | Duration | GENERATION PARAMETER | Set in pipeline; Hypogeum uses 5s [L1 config fact], model supports 4–15s [L1] |
| §33.1 | One primary focus | SEMI-EXPLICIT | One subject per time segment [L3 structure; L5 fidelity] |
| §33.2 | One primary action | EXPLICIT | One action verb per time segment [L3 structure; L5 fidelity] |
| §34.1 | Episode continuity | TRANSLATION DISCIPLINE + @REFERENCE | @Reference syntax [L3] + vocabulary consistency [L5] |

---

## 11. Verified Examples

### Example 1 — Camera: Poor vs. Canonical

**POOR:**
> *"The camera slowly orbits while pushing in and panning right to keep the subject centered."*

**Why it fails:** Three concurrent movements ("orbits", "pushing in", "panning right"). The community skill explicitly identifies conflicting camera instructions as a failure mode. [L3: SEED-COMM-01]

**CANONICAL:**
> *"Camera: orbit shot, slow, around the mechanism. Subject: mechanism stationary under directional light."*

**Why it works:** One camera movement term (`orbit shot`), from the documented vocabulary, with one speed qualifier. [L3: SEED-COMM-01]

**Evidence:** SEED-COMM-01 [Level: L3]

---

### Example 2 — @Reference: Vague vs. Explicit

**POOR:**
> *"@Video1 @Image1 — recreate this scene with the character"*

**Why it fails:** No explicit role assignment for either asset. The community skill identifies this as "vague references" — the model cannot determine what to reference. [L3: SEED-COMM-01]

**CANONICAL:**
> *"@Image1's character as the subject. Reference @Video1's camera movement. The character walks through the ancient corridor."*

**Why it works:** Explicit role declarations for both assets — character from image, camera behavior from video. [L3: SEED-COMM-01]

**Evidence:** SEED-COMM-01 [Level: L3]

---

### Example 3 — Lighting: Mood vs. Physical

**POOR:**
> *"Dark and moody underground lighting with dramatic shadows."*

**Why it fails:** Mood vocabulary without physical anchor. [L5: extrapolated from general guidance — not tested for Seedance specifically]

**CANONICAL:**
> *"Warm amber directional light from the upper left, illuminating the stone vault face; deep shadow on the opposing side. Cinematic quality, photorealistic."*

**Why it works:** Physical direction and character named; style descriptor used as per community skill vocabulary. [L5 for lighting response; L3 for cinematic/photorealistic vocabulary]

**Evidence:** SEED-COMM-01 [Level: L3 for style vocabulary]; SEED-HYP-01 [Level: L5 for lighting response]

---

### Example 4 — Audio: Omission vs. Declared Intent

**POOR:**
> *[Prompt describes a crowd of Roman workers in a hypogeum — no audio parameter set, no audio declaration]*

**Why it fails:** Seedance generates audio by default (`audio: true`). Without explicit suppression, the model generates crowd noise, ambient sounds, and possibly music, which compete with the pipeline's narration and licensed music.

**CANONICAL:**
> *API request: set `audio: false`. If MUAPI path does not expose this parameter: include in prompt: `Sound design: natural stone acoustic ambience only. No crowd sounds. No music. No speech.`*

**Why it works:** API parameter suppresses audio generation at the model level. [L1: SEED-API-01 for parameter existence; REQUIRES VERIFICATION: V-01 for MUAPI path; L5 for prompt-based fallback effectiveness]

**Evidence:** SEED-API-01 [Level: L1]

---

## 12. Verification Queue

| # | Behavior | Section | Suggested Verification Method |
|---|---|---|---|
| V-01 | Whether the `audio` boolean parameter is accessible via the Hypogeum MUAPI path for Seedance | §6.1 (C-03), §7.14 | Check MUAPI Seedance payload structure; test with `audio: false` if exposed. If confirmed, establishes L1 fact for MUAPI path. |
| V-02 | Whether CGI-appearance suppression is achievable through positive description for Seedance | §7.12 | Generate matched prompts: generic material vs. period-specific; evaluate CGI-register output. Formal experiment required for L4. |
| V-03 | Physical description vs. mood language — whether Seedance produces more specific output with physical descriptions | §6.3 | Controlled test: identical scene content with mood vs. physical description. Formal experiment required for L4. |
| V-04 | Camera movement fidelity — whether single movement terms produce consistent, repeatable motion | §7.5 | 10-generation test per movement type; evaluate consistency. Formal experiment required for L4. |
| V-05 | Material specificity — whether period-specific material descriptions improve surface quality | §7.10 | Side-by-side: generic noun vs. full period material description. Formal experiment required for L4. |
| V-06 | Temporal phase structure — whether time-segmented format is honored reliably | §7.13 | Frame-by-frame review of 5s clips with explicit phase descriptions. Formal experiment required for L4. |
| V-07 | Subject positioning — whether positional language controls frame composition | §7.7 | Test with and without positional instruction; evaluate subject placement. Formal experiment required for L4. |
| V-08 | Negative constraint effectiveness — whether any negative constraint format works for Seedance | §4.7, §7.11 | Test "no X" phrasing, descriptor exclusion, and positive-only approaches on period scenes. Formal experiment required for L4. |
| V-09 | Front-weighted prompt processing — whether earlier elements receive more weight | §6.3 | Element position swap test. Formal experiment required for L4. |
| V-10 | Atmospheric specificity — whether anchored atmospheric descriptions produce spatial atmospheric behavior | §7.4 | Side-by-side: "smoky atmosphere" vs. "torch smoke at vault ceiling in light beam." Formal experiment required for L4. |
| V-11 | Default vignette or other post-processing artifacts | §13 (failure modes) | Review multiple generated clips for consistent artifacts. Formal experiment required for L4. |
| V-12 | @Reference character consistency for same-setting clips | §7.16 | Multi-generation test with consistent @Image reference. Formal experiment required for L4. |

---

## 13. Failure Modes

### FM-01 — Audio Default Generation

**Description:** Seedance generates sound effects and background music by default. Audio is not suppressed by omitting audio direction from the prompt.

**Evidence:** SEED-API-01 [Level: L1 — `audio` parameter default: `true`]

**Impact:** Inferred audio competes with pipeline's narration and licensed music tracks.

**Suppression:** Set `audio: false` via API parameter [L1: SEED-API-01 — parameter exists]. MUAPI path accessibility: REQUIRES VERIFICATION (V-01).

---

### FM-02 — Stacked Movement Conflict

**Description:** When conflicting camera instructions are provided in the same segment, the model cannot execute both reliably.

**Evidence:** SEED-COMM-01 [Level: L3 — explicitly documented as a mistake in the community skill]

**Impact:** Unpredictable camera output; scene's narrative function not served.

**Suppression:** One camera movement term per time segment (§7.5). [L3 for the recommendation; L5 for confirmed behavioral effect]

---

### FM-03 — Vague @Reference Behavior

**Description:** When an uploaded asset is not given an explicit role assignment, the model cannot determine how to use it.

**Evidence:** SEED-COMM-01 [Level: L3 — "vague references" explicitly listed as a common mistake]

**Impact:** Reference assets not used as intended; character inconsistency, wrong camera behavior.

**Suppression:** Explicit role declaration for every uploaded asset using documented @Reference syntax (§4.6, §5.5). [L3 for the recommendation]

---

### FM-04 — Segment Overloading

**Description:** Packing too many scenes, actions, or transitions into a short time segment produces output that cannot honor all instructions.

**Evidence:** SEED-COMM-01 [Level: L3 — "overloading" explicitly listed as a common mistake]

**Impact:** Multiple instructions dropped; output satisfies only part of the intent.

**Suppression:** Keep prompt complexity proportional to clip length (§9, AP-07). [L3 for the recommendation]

---

### FM-05 — Anachronism Injection

**Description:** Period scenes may include modern elements. L5 for Seedance — no documentation confirms this behavior, but it is a general risk for historical content generation.

**Evidence:** L5 (extrapolated from general AI video generation behavior)

**Impact:** §17 violations.

**Suppression:** Positive period description approach (§7.11) — [L5] for effectiveness.

---

### FM-06 — Material Register Flattening

**Description:** Generic material descriptions may produce smooth, uniformly lit, CGI-register surfaces. L5 for Seedance — not documented specifically.

**Evidence:** SEED-HYP-01 [Level: L5]

**Impact:** §16 and §18 violations.

**Suppression:** Period-specific material description with surface condition and light behavior — [L5] for effectiveness (V-05).

---

## 14. Capability Matrix

| Feature | Status | Notes |
|---|---|---|
| T2V (text-to-video) | SUPPORTED [L1: SEED-API-01] | `seedance2`, `seedance2_fast`, `seedance2_mini` via Runway API; MUAPI path also |
| I2V (image-to-video) | SUPPORTED [L1: SEED-API-01] | Runway: `promptImage`; MUAPI: `images_list` array [L1: SEED-HYP-01] |
| Duration: 4–15s (model capability) | SUPPORTED [L1: SEED-API-01, SEED-COMM-01] | Full range |
| Duration: 5s (Hypogeum MUAPI config) | CONFIGURED [L1: SEED-HYP-01] | Hypogeum pipeline choice; not a model limit |
| Duration: 8s (channel standard) | SUPPORTED [L1: SEED-API-01] | Within model range; not current Hypogeum configuration |
| Aspect ratio: 16:9 (1280×720) | SUPPORTED [L1: SEED-API-01] | All tiers |
| Aspect ratio: 9:16 (720×1280) | SUPPORTED [L1: SEED-API-01] | All tiers |
| Aspect ratio: 1:1 (960×960) | SUPPORTED [L1: SEED-API-01] | All tiers |
| Aspect ratio: 4K | SUPPORTED [L1: SEED-API-01] | Full model (`seedance2`) only |
| Audio generation | default-on [L1: SEED-API-01] | `audio` boolean, default `true` |
| Audio suppression (Runway API) | SUPPORTED [L1: SEED-API-01] | `audio: false` parameter |
| Audio suppression (MUAPI path) | REQUIRES VERIFICATION (V-01) | Parameter accessibility not confirmed |
| @Reference system (images) | SUPPORTED [L3: SEED-COMM-01] | Up to 9 images; role assignment required; consistency effectiveness is L5 |
| @Reference system (video) | SUPPORTED [L3: SEED-COMM-01] | Up to 3 videos; role assignment required |
| @Reference system (audio) | SUPPORTED [L3: SEED-COMM-01] | Up to 3 audio files |
| Time-segmented prompts | SUPPORTED [L3: SEED-COMM-01] | Documented for 10s+ clips; segment fidelity is L5 |
| Camera movement vocabulary | DOCUMENTED [L3: SEED-COMM-01] | Terms appear in community skill attributed to official ByteDance docs; behavioral reliability is L5 |
| Negative constraint format | NOT OFFICIALLY DOCUMENTED | No format specified in available sources |
| Keyframe interpolation (first+last) | SUPPORTED [L1: SEED-API-01] | Runway API: `position: "first"/"last"` — cannot mix with reference mode |

---

## 15. Future Extensions

### 15.1 Priority Verification Work

1. **V-01** (MUAPI audio parameter) — highest priority: §21.3 compliance cannot be confirmed until it is known whether `audio: false` is accessible via MUAPI. If confirmed, establishes L1 for MUAPI path.
2. **V-08** (negative constraint format) — blocking for §17 and §18: no suppression mechanism is confirmed for Seedance
3. **V-03** (physical vs. mood description) — needed to confirm or deny the vast majority of L5 translation rules; would require formal L4 experiment
4. **V-04** (camera movement fidelity) — needed before camera vocabulary from §7.5 becomes architectural; L3 recommendation would be supported by L4 observation

### 15.2 L4 Promotion Path

No items in this skill currently qualify for L4 — SEED-HYP-01 has no documented experiment IDs, prompts, or controlled behavioral outputs. To promote any Section C hypothesis to L4, a formal experiment must be conducted with documented: (a) experiment ID, (b) prompt used, (c) output description, (d) statement of what the observation demonstrates. V-01 through V-12 are the priority test cases.

### 15.3 Duration Reconfiguration Consideration

The Hypogeum pipeline currently uses 5-second clips for Seedance via MUAPI. The model supports 4–15 seconds. If the pipeline is reconfigured to 8 seconds:
- Coverage math changes: 38 clips × 8s = 304s (vs. 60 clips × 5s = 300s — similar total coverage)
- Temporal structure adapts to 8-second phase splits (consistent with §19.2)
- Cost implications are outside this skill's scope

### 15.4 @Reference System for Hypogeum Episodes

The `@Reference` system enables character and scene consistency across clips within an episode. This is directly applicable to §34 (Cross-Episode Visual Continuity). Priority documentation work: establish which reference assets to use for each episode setting, and how to structure the @Reference declarations in the CPA for Seedance-bound generations.

### 15.5 Version History

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-08 | Initial creation. 12 sections. Template-compliant (Phase 0.7/0.8). |
| 2.0 | 2026-07-08 | Phase 0.9 evidence-based refactor. Evidence Registry added. Three-section structure (A/B/C). Rule Traceability added. Major corrections: duration corrected from "5s fixed" to 4–15s model capability (5s is Hypogeum configuration); audio parameter confirmed as boolean, default-on via Runway API; @Reference system documented (critical feature missing from v1.0); §4.13 Duration Adaptation removed (was based on incorrect 5s-only assumption). Most behavioral claims downgraded to HYPOTHESIS. Negative constraint format confirmed as NOT OFFICIALLY DOCUMENTED. |
| 3.0 | 2026-07-08 | Phase 1.0 evidence taxonomy refactor. Confidence + Status fields replaced with single Level field throughout. Evidence Registry Max Claim Level column added. Capability Matrix status values updated to L1/L2/L3. B-items reclassified: API capability facts (B-01, B-02, B-03, B-04, B-05, B-06, B-07, B-08, B-10) → L1; community-documented behaviors (B-09, B-11, B-12, B-13) → L3. All Section C items → L5 or L1 for configuration facts. Translation table inline markers updated to [L1]/[L3]/[L5]. Rule Traceability Table: Verification Status column → Level column. No items qualify as L2 (no Seedance official API documentation for prompt syntax) or L4. |
