# Seedance Translation Skill — Compact Runtime Reference
<!-- SOURCE: hypogeum/skills/seedance_translation_skill.md v3.0 | COMPACT: B1 -->
<!-- PURPOSE: Runtime-only condensed version. Full skill retained for audit/reference. -->

## Provider Identification

**Provider:** Seedance 2.0 (ByteDance / Jimeng AI)

MUAPI production identifiers:
- `seedance-v2.0-t2v` — T2V path
- `seedance-v2.0-i2v` — I2V path

## Supported Parameters [L1: SEED-API-01]

| Parameter | Value | Source |
|---|---|---|
| Durations | 4–15s (model); **5s** (Hypogeum MUAPI config — pipeline choice, not limit) | SEED-API-01, SEED-HYP-01 |
| Aspect ratios | 16:9 (1280×720), 9:16 (720×1280), 1:1 (960×960), 4K (full model only) | SEED-API-01 |
| Text input limit | 3,500 characters (Runway API `promptText`) | SEED-API-01 |
| Audio | **default-on** — `audio` boolean, default `true` | SEED-API-01 |
| I2V input format (MUAPI) | `images_list` array | SEED-HYP-01 (verified 2026-06-06) |
| I2V input format (Runway API) | `promptImage` — single or array; `position: "first"/"last"` for keyframe mode | SEED-API-01 |

## Hard Rules

**AUDIO IS DEFAULT-ON** [L1: SEED-API-01]
Seedance generates sound effects and background music when `audio` is `true` (default). This competes with pipeline narration and music.

Primary suppression: Set `audio: false` in API request [L1: SEED-API-01 — parameter exists].
MUAPI path accessibility: **REQUIRES VERIFICATION (V-01)** — whether MUAPI exposes this parameter is unconfirmed.

Fallback (if MUAPI does not expose parameter): Include in prompt `[Audio/Sound Design]` element:
> `Sound design: natural stone acoustic ambience only, no music, no speech.`
Note: prompt-based fallback effectiveness is [L5].

**NO STACKED CAMERA MOVEMENTS** [L3: SEED-COMM-01]
Community skill explicitly identifies "conflicting instructions — don't ask for 'static camera' AND 'orbit shot' in same segment" as a mistake.
Use **one camera movement term per time segment**. Whether stacking reliably degrades output is L5 pending L4 testing.

**@REFERENCE: ALWAYS SPECIFY ROLE** [L3: SEED-COMM-01]
Community skill explicitly identifies "vague references — don't say 'reference @Video1' without specifying WHAT to reference" as a failure mode. Always append role type.

**NEGATIVE CONSTRAINTS: NOT OFFICIALLY DOCUMENTED** [NOT OFFICIALLY DOCUMENTED]
No official or community source specifies a negative constraint format for Seedance. Use positive description approach for all exclusions.

## @Reference System Syntax [L3: SEED-COMM-01]

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

Note: Consistency effectiveness for all @Reference roles is [L5] pending L4 testing.

## Official Prompt Structure [L3: SEED-COMM-01]

**8-part formula:**
```
[Subject/Character Setup] + [Scene/Environment] + [Action/Motion Description] +
[Camera Movement] + [Timing Breakdown] + [Transitions/Effects] +
[Audio/Sound Design] + [Style/Mood]
```

**Temporal structure (5s Hypogeum clips):**
```
0-2.5s: [opening physical state]   2.5-5s: [physical change from opening state]
```

**For 10s+ clips (time-segmented format):**
```
0–3s: [opening scene]   3–6s: [development]   6–10s: [climax]   10–15s: [resolution]
```
Note: Segment fidelity is [L5] pending L4 testing.

## Camera Vocabulary [L3: SEED-COMM-01]

**Movements:**

| Term | Use for |
|---|---|
| Push in / Slow push | Approaching subject |
| Pull back / Pull away | Withdrawing |
| Pan left / Pan right | Surveying |
| Tilt up / Tilt down | Vertical reframing |
| Track / Follow shot | Following movement |
| Orbit / Revolve | Encircling subject |
| Crane shot | Vertical arc movement |
| First-person POV | Subjective exploration |
| Static camera | Establishing hold |

Note: [L3] means terms appear in community skill attributed to official ByteDance docs. Behavioral reliability is [L5].

**Shot scales:** extreme close-up, close-up, medium close-up, medium shot, full shot, wide shot / establishing shot, bird's eye view / overhead, low angle

## Translation Rules (§7 Condensed)

| §Ref | Requirement | Seedance Expression | Level |
|---|---|---|---|
| §7.1 | Channel palette | Colors through light + material description | [L5] |
| §7.2 | Warm key light | `warm amber light from upper left, raking across surface` | [L5] |
| §7.3 | Near-black shadows | `deep shadow zones with directional falloff` | [L5] |
| §7.4 | Atmospheric evidence | `stone dust in the light beam`, `torch smoke at vault ceiling` | [L5] |
| §7.5 | Single motivated movement | One movement term per segment (§11.2) | [L3] vocab; [L5] reliability |
| §7.6 | Shot scale | Community-documented shot size vocabulary | [L3] vocab; [L5] reliability |
| §7.7 | Subject positioning | `subject positioned in the right half of the frame` | [L5] |
| §7.9 | No void backgrounds | Name ≥1 physical environmental element | [L5] |
| §7.10 | Material specificity | `travertine — raking light on raised face, shadow in recessed joints` | [L5] |
| §7.11 | No modern elements (§17) | Positive: `ancient stone construction, period-accurate materials` | [L3] approach; [L5] effectiveness |
| §7.11 | No frontal faces (§17.3) | `figures seen from behind, silhouetted against the light` | [L5] |
| §7.12 | No CGI register (§18) | `cinematic quality, photorealistic` + period material specificity | [L3] vocab; [L5] effectiveness |
| §7.13 | Temporal structure | `0-2.5s: [opening]. 2.5-5s: [change].` (5s config) | [L3] format; [L5] fidelity |
| §7.14 | Audio policy | `audio: false` API parameter; fallback prompt declaration | [L1] parameter; [L5] prompt fallback |
| §7.15 | One primary focus (§33) | One subject + one action per time segment | [L3] structure; [L5] fidelity |
| §7.16 | Episode continuity (§34) | @Reference for character/scene + identical vocabulary anchors | [L3] syntax; [L5] effectiveness |

## Key Failure Modes

| FM | Trigger | Fix |
|---|---|---|
| FM-01 | Audio default-on → inferred crowd noise/music | Set `audio: false` via API parameter; confirm MUAPI path accessibility (V-01) |
| FM-02 | Stacked movements in one segment → unpredictable camera | One movement term per segment [L3: SEED-COMM-01] |
| FM-03 | Vague @Reference without role → asset unused as intended | Explicit role declaration for every asset [L3: SEED-COMM-01] |
| FM-04 | Overloaded short segment → instructions dropped | One action per 5s clip [L3: SEED-COMM-01] |
| FM-05 | Period scene → anachronism injection | Positive period description approach [L5] |
| FM-06 | Generic material → CGI register | Period-specific material with surface condition + light behavior [L5] |

## Anti-Patterns

| AP | Pattern | Fix |
|---|---|---|
| AP-01 | Stacked movements: `static camera` + `orbit shot` same segment | One movement per segment |
| AP-02 | Contradictory adjectives: `dynamic but subtle` | Commit to one direction |
| AP-03 | Mood without physical anchor: `mysterious and ancient` | Physical observations only |
| AP-04 | Generic material: `stone`, `metal` | Name material + surface condition + light behavior |
| AP-05 | No audio suppression when scene implies audio | Set `audio: false` or include audio design element |
| AP-06 | Vague @Reference: `reference @Video1` (no role) | Always specify role: `reference @Video1's camera movement` |
| AP-07 | Overloaded prompt for 4–5s clip | One clear action per clip |

## Unresolved Verifications

| ID | Behavior | Status |
|---|---|---|
| V-01 | `audio` boolean accessible via Hypogeum MUAPI path | **REQUIRES VERIFICATION** — confirm before V3 (Video Brand Guardian) |
| V-08 | Any negative constraint format works for Seedance | **REQUIRES VERIFICATION** — L5; blocking for §17/§18 compliance |
| V-04 | Camera movement vocabulary executes as intended cinematically | **REQUIRES VERIFICATION** — L3 recommendation, L5 behavioral fidelity |
| V-03 | Physical description produces more specific output than mood language | **REQUIRES VERIFICATION** — L5; needed to confirm most §7 translation rules |
