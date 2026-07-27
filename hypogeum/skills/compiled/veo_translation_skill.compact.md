# Veo Translation Skill — Compact Runtime Reference
<!-- SOURCE: hypogeum/skills/veo_translation_skill.md v3.0 | COMPACT: B1 -->
<!-- PURPOSE: Runtime-only condensed version. Full skill retained for audit/reference. -->

## Provider Identification

**Provider:** Veo 3 (Google DeepMind)

MUAPI production identifiers:
- `veo3-text-to-video` — T1 (premium/hero scenes)
- `veo3-fast-text-to-video` — T2/T3 (primary production and cheap-mode)
- `veo3-image-to-video` — I2V path

## Supported Parameters [L1]

| Parameter | Value | Source |
|---|---|---|
| Durations | 4s, 6s, or 8s — Hypogeum configures **8s** (pipeline choice, not model limit) | VEO-DOC-02 |
| Aspect ratios | 16:9 (1280×720), 9:16 (720×1280) | VEO-DOC-03 |
| Text input limit | 1,024 tokens | VEO-DOC-04 |
| Audio | **default-on** — generated natively | VEO-DOC-01 |
| I2V input format (MUAPI) | `images_list` array | VEO-HYP-01 |
| Reference images | Up to 3 (character/style/setting consistency) | VEO-DOC-01 |

## Hard Rules

**AUDIO IS DEFAULT-ON** [L1: VEO-DOC-01]
Every prompt MUST include a dedicated audio declaration sentence. Omission causes Veo to infer and generate crowd noise, music, sound effects. This is a production failure (§AP-05).

Canonical audio declaration:
> `Ambient noise: natural stone acoustic resonance and distant environmental sounds. Sound design: ambient environmental audio only, speech absent, music absent.`

Use `Ambient noise:` prefix [L2: VEO-DOC-01] + descriptor-form exclusion — NOT `[no speech]`.
Ambient-only suppression effectiveness: **REQUIRES VERIFICATION (V-05)** [L5].

**`[no X]` FORMAT NOT RECOMMENDED** [L2: VEO-DOC-01, VEO-DOC-02]
Official guidance: *"We recommend that you don't use instructive language such as 'no' or 'don't'."*
Use descriptor approach: name what to exclude as nouns/descriptors integrated into positive description.

| Prohibited | Recommended |
|---|---|
| `[no vignette]`, `[no frontal faces]` | Descriptor: `level camera horizon`, `figures seen from behind` |
| `[no modern elements]` | Positive: `ancient stone construction, period-accurate materials` |
| `[no ambient fill]` | Positive: `directional key light from one side, deep shadow zones` |

> **Hypogeum Policy Note (2026-07-17, Phase B1.2A):** This table is the confirmed source of a platform-wide finding — negative/forbidden-rule expression syntax is provider-specific, not universal. Veo's own guidance discourages the bracketed `[no X]` form that `cinematic_prompt_architecture.md` (Layer 9) and Video DNA's Forbidden-section rendering currently use unconditionally for every provider. This is not corrected here — the fix belongs to a future Provider Adapter's translation layer (Phase B3), which must render each DNA module's canonical forbidden-item list into whatever syntax the target provider actually prefers, rather than one bracket-tag format assumed to be universally valid. See `PHASE_B_IMPLEMENTATION_ROADMAP.md` for the amendment this finding produced.

**ONE CAMERA MOVEMENT PER PROMPT** [L5: C-02]
Stacking movements produces inconsistent output. One official movement term + one speed qualifier (§AP-01).

**DUTCH ANGLE** [L2: VEO-DOC-01]
Listed in official Veo docs. Channel prohibits it (§11.3) — channel rule, not provider limitation.
Suppression: `level camera horizon` as part of cinematography [L5 for effectiveness — V-08].

## Official Prompt Structure [L2: VEO-DOC-01, VEO-DOC-02]

```
[Cinematography] + [Subject] + [Action] + [Context] + [Style & Ambiance]
```

Temporal structure (8s clips):
```
[00:00-00:04] [opening physical state]  [00:04-00:08] [physical change from opening state]
```
Note: `[00:00-00:04]` is the official format [L2]. Previous `0-4s:` format is NOT official.

## Camera Vocabulary [L2: VEO-DOC-01]

**Movements:** static shot, pan (left/right), tilt (up/down), dolly (in/out), truck (left/right), pedestal (up/down), zoom (in/out), crane shot, aerial/drone shot, handheld, whip pan, arc shot, tracking shot, slow pan, POV shot

> **Hypogeum Policy Note:** "static shot" is genuine, officially-supported Veo vocabulary — listed here as a provider fact, not a recommendation. Hypogeum's own canonical static-framing policy (owned by `hypogeum/camera_dna.py` as of Phase B2.5, see `docs/validation/prompt_writer_architecture/B1_2A_STATIC_CAMERA_POLICY_MEMO.md`) constrains when it may be used: only with meaningful subject, environmental, atmospheric, or lighting motion in frame, and only when intentional, rare, and narratively justified — never as a default. This note does not redefine that policy, only points to it.

**Angles / Shot scales:**
| Term | §Ref |
|---|---|
| extreme close-up, close-up, medium close-up, medium shot | §12 |
| full shot / long shot, wide shot / establishing shot | §12 |
| low-angle shot, high-angle shot, bird's-eye / top-down | §11 |
| over-the-shoulder, POV shot, worm's-eye view | §11 |

**Depth of field:** `shallow depth of field` / `deep depth of field` [L2]

## Translation Rules (§7 Condensed)

| §Ref | Requirement | Veo Expression | Level |
|---|---|---|---|
| §7.1 | Channel palette | Colors emerge from light + material — no hex parameter | [L5] |
| §7.2 | Warm key light | `warm amber light from upper left, raking across surface; hard falloff into shadow` | [L5] |
| §7.3 | Near-black shadows | `deep shadow zones`, `hard falloff into darkness` | [L5] |
| §7.4 | Atmospheric evidence | `stone dust in the light beam`, `torch smoke drifting upward at vault ceiling` | [L5] |
| §7.5 | Single motivated movement | One official movement term + speed qualifier | [L2] vocab; [L5] reliability |
| §7.6 | Shot scale | Official angle/shot scale vocabulary | [L2] |
| §7.7 | Subject positioning | Positional language: `subject in right half of frame` | [L5] |
| §7.9 | No void backgrounds | Name ≥1 physical environmental element | [L5] |
| §7.10 | Material specificity | `travertine — raking light on raised face, shadow in recessed joints` | [L5] |
| §7.11 | No modern elements | Positive: `period-accurate materials, practical torch lighting` | [L2] format; [L5] effectiveness |
| §7.11 | No frontal faces (§17.3) | `figures seen from behind or in silhouette` | [L2] format; [L5] effectiveness |
| §7.12 | No CGI register | `documentary register, photorealistic, practical light sources` | [L2] vocab; [L5] effectiveness |
| §7.13 | Temporal structure | `[00:00-00:04]` / `[00:04-00:08]` official timestamp format | [L2] syntax; [L5] segment fidelity |
| §7.14 | Audio policy | Dedicated `Ambient noise:` sentence; descriptor-form exclusions | [L2] syntax; [L5] suppression (V-05) |
| §7.16 | Episode continuity | Reuse identical vocabulary anchors across same-setting clips | [L5] — no provider memory mechanism |

## Key Failure Modes

| FM | Trigger | Fix |
|---|---|---|
| FM-01 | Audio not declared → Veo infers crowd noise/music | Add `Ambient noise:` sentence to EVERY prompt |
| FM-02 | No compositional instruction → center-frame default | Use positional language explicitly [L5] |
| FM-03 | No directional source → diffuse ambient default | Name source character, direction, falloff [L5] |
| FM-04 | Period scenes → modern element injection | Descriptor-form exclusion for likely modern elements [L5] |
| FM-05 | Human figures → frontal face default | `figures from behind or in silhouette` [L5] |
| FM-06 | Long prompt → late-element dropout near 1,024 token limit | Priority elements early in prompt [L5] |

## Anti-Patterns

| AP | Pattern | Fix |
|---|---|---|
| AP-01 | Stacked movements: `dolly in while panning right` | One movement per prompt |
| AP-02 | Contradictory adjectives: `dynamic but subtle` | Commit to one direction |
| AP-03 | Mood vocabulary without physical anchor: `mysterious and ancient` | Physical observations only |
| AP-04 | Generic material: `old stone` | Name material + surface condition + light behavior |
| AP-05 | No audio sentence | ALWAYS include audio declaration |
| AP-06 | `[no X]` format | Descriptor approach (see Hard Rules) |
| AP-07 | Sourceless lighting: `warm atmospheric glow fills the space` | Name source, direction, what it illuminates, falloff |

## Unresolved Verifications

| ID | Behavior | Status |
|---|---|---|
| V-05 | `Ambient noise:` declaration reliably suppresses music/speech/SFX | **REQUIRES VERIFICATION** — currently L5 |
| V-07 | Veo applies default vignette; best suppression approach | **REQUIRES VERIFICATION** — L5 |
| V-08 | `level camera horizon` suppresses dutch angle | **REQUIRES VERIFICATION** — L5 |
| V-01 | Stacked movements consistently produce unreliable output | L5 pending L4 experiment |
