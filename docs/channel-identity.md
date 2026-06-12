# The Hypogeum Chronicles — Channel Identity

**Version:** v1.0  
**Status:** Operational reference. Derived from Brand Bible v1.1 (`docs/brand_guardian/brand_bible_hypogeum.md`).  
**Purpose:** Single-file identity reference for the production pipeline. Gates and agents enforce from this document.  
**Last updated:** 2026-06-12

---

## 0. Enforcement Status (Governance Note)

The rules in this document are the authoritative identity specification. Not all rules
are enforced in code. The current enforcement coverage is **partial**.

| Rule Category | Enforcement Status |
|---|---|
| No human face (thumbnail) | **Enforced** — BGA Pass 2 (text scan) + TIG (vision gate) |
| No bright/high-key background (thumbnail) | **Enforced** — BGA Pass 2 (text scan) + TIG (vision, 0–40% brightness) |
| No neon/cyberpunk/sci-fi | **Enforced** — BGA Pass 1/2 (text scan) + TIG (vision) |
| No talking head / direct-to-camera | **Enforced** — BGA Pass 1 (text scan) |
| Wordmark present and legible | **Enforced** — BGA Pass 2 (text scan) + TIG (vision) |
| Warm earth tone palette (video/thumbnail) | **Prompt-only** — VPD + TD system prompts; BGA catches obvious violations in text |
| Color palette hex values (#080C0E, etc.) | **Not enforced** — hex validation in generated output is deferred |
| Typography: Cinzel SemiBold / Raleway Regular | **Prompt-only when font is named** — BGA blocks wrong fonts only if explicitly named in prompt; actual rendered font is never validated |
| Font weights, hierarchy, line spacing | **Not enforced** — acknowledged unenforceable at generation time |
| BGA Pass 3 (post-render pixel audit) | **Deferred** — no implementation timeline |

For the complete gap register, see `docs/factory-governance-gaps.md`.

## Document Hierarchy

This document is the **single authoritative operational identity source** for The Hypogeum Chronicles production pipeline. The other identity documents exist for specific subordinate purposes:

| Document | Role | Status |
|---|---|---|
| `docs/channel-identity.md` ← **this file** | **Operational identity source.** The canonical definition of all visual, typographic, and content identity rules. All agents and enforcement gates derive from this document. | Operational |
| `docs/brand_guardian/brand_spec_executable.md` | **Enforcement mapping.** Defines which rules in this document are enforceable in code/LLM, which are INFERRED, and which are NOT_IN_IMAGE. Consumed when designing or updating enforcement gates. | Enforcement reference |
| `docs/brand_guardian/brand_bible_hypogeum.md` | **Historical extraction artifact.** The original visual extraction from `bible.png`. Activation checklist is not complete. Not independently enforced — superseded by this document for all operational purposes. | Inactive (historical) |
| Agent system prompts (`hypogeum/prompts/*.txt`) | **Derived runtime artifacts.** Written based on this document. Not independent sources of truth. When this document changes, the relevant prompt(s) must be updated manually until single-source injection is built. | Derived |

> **Important:** As of 2026-06-12, no agent reads this file at runtime. All enforcement derives from the agent system prompt files, which were written based on this document. Single-source runtime injection is a planned future capability (see `docs/factory-governance-gaps.md`).

---

## 1. Visual Palette

Seven-swatch palette. Warm earth and gold tones only. No cool colors. No bright colors.

| Swatch | Hex | Role |
|---|---|---|
| void_black | `#080C0E` | Primary background |
| deep_stone | `#1A1D20` | Secondary background |
| charcoal_earth | `#2B2E33` | Surface / mid-depth |
| ancient_soil | `#5C4A2F` | Warm dark accent |
| antique_gold | `#C49A42` | Primary accent |
| warm_gold | `#E7D6B3` | Primary text accent |
| parchment_cream | `#F5F3EE` | Primary light text |

> Hex values are verified from the approved Style Board (2026-06-08). All 7 values are canonical.

### Color Rules

- All surfaces must use palette swatches only.
- Text on dark backgrounds: warm_gold (`#E7D6B3`) for display/titles; parchment_cream (`#F5F3EE`) for body.
- Video overlay opacity for dark overlays: 0.70–0.92.
- Minimum contrast: WCAG AA. Gold text on near-black background.

### Forbidden Colors

Bright white · Neon hues (any) · Cool blues · Greens · Pinks · Purples · Bright oranges

These apply to all outputs. A period-accurate palette may not use globally forbidden colors even if historically justified.

---

## 2. Thumbnail Rules

### Composition

- **Wordmark-dominant.** The channel wordmark (THE HYPOGEUM CHRONICLES) is the primary compositional anchor.
- Background: full-bleed atmospheric image. Dark tone (void_black or deep_stone dominant, 0–40% brightness).
- Subject matter: stone ruins, underground architecture, maps, carvings, arched corridors.
- Slight vignette or darkening at edges to anchor the wordmark.

### Required Elements

1. Channel wordmark with full three-line hierarchy (mandatory).
2. Atmospheric dark background image.
3. Episode-specific subtitle in Raleway (optional but standard).

### Dimensions

- Required: **1280 × 720 px** (YouTube standard).
- Aspect ratio: 16:9.

### Thumbnail Color Bounds

- Background: void_black or deep_stone (dominant).
- Accent: warm_gold or antique_gold.
- Text: warm_gold (title) + parchment_cream (subtitle).

**Forbidden thumbnail colors:** bright white, pure red, neon (any hue), cool blue, green, purple, pink, bright orange.

### Face Rule

**No human faces in thumbnails.** Environmental, architectural, and artefact imagery only.

### Wordmark Legibility

Wordmark must be legible at thumbnail scale. The em-dashes flanking `— CHRONICLES —` are mandatory and must not be omitted or substituted.

### Forbidden Thumbnail Compositions

- Human face as primary subject
- Bright or high-key backgrounds
- Layout that disrupts wordmark hierarchy
- Subtitle text dominating over HYPOGEUM scale
- Episode imagery minimising or subordinating the channel wordmark
- Light or neutral backgrounds

---

## 3. Typography and Text-Overlay Rules

### Two-Typeface System

No typefaces other than these two are permitted in any output.

| Typeface | Weight | Classification | Use |
|---|---|---|---|
| Cinzel | SemiBold | Classical serif (Roman inscriptional) | All display text: channel name, episode titles, thumbnail headlines, section headers |
| Raleway | Regular | Geometric sans-serif | All body text: subtitles, taglines, captions, Shorts overlays, metadata copy |

### Thumbnail Typography

| Property | Rule |
|---|---|
| Headline font | Cinzel SemiBold |
| Headline case | ALL CAPS |
| Headline tracking | Wide |
| Headline max words | 4 |
| Headline max characters | 30 |
| Subtitle font | Raleway Regular |
| Subtitle max words | 7 |

### Shorts Overlay Typography

- Font: Raleway Regular (not Bold)
- Max words per overlay: 7 (hard limit)
- Case: ALL CAPS
- Safe zone margin: TBD

### Wordmark Structure

```
THE                          ← small caps, wide tracking, subordinate
HYPOGEUM                     ← primary display, full weight, dominant scale
— CHRONICLES —               ← medium scale, em-dash flanking, equal weight to line 1
```

The em-dashes flanking CHRONICLES are mandatory. Never substitute with hyphens. Never omit.

### Forbidden Typefaces

Any typeface not in the two-font system. Specific examples: Comic Sans, Impact, Arial, Helvetica (as display), script/handwriting fonts.

### Forbidden Text Treatments

Bold Raleway in overlays · Lowercase Cinzel (small caps only) · Mixed case in display text · Drop shadows on text · Outlined text strokes · Gradient fills on text

---

## 4. Forbidden Visual Patterns

### Absolute Prohibitions

No agent, prompt, config flag, or narrative justification can authorize these.

**Visual modes:**
- Talking head
- Visible lip sync
- Interview framing
- Characters speaking to camera
- Mouth movement synchronized with narration
- Direct address to camera

**Composition:**
- Human faces as primary thumbnail subject
- Bright or high-key backgrounds in any branded material
- Any composition that minimises or subordinates the channel wordmark

**Content:**
- Fabricated historical footage presented as authentic
- Synthesized voice or likeness of named historical or living individuals
- Disputed interpretation presented as settled historical fact

**Lighting:**
- Flat even lighting
- Overexposed bright scenes
- Neon colored lighting
- Ring light aesthetics

**Motion:**
- Rapid cuts or montage sequences
- Camera shake or instability
- Speed variations / whip pans / zoom bursts
- Social-media style snap cuts

### Emotional Register Violations

Forbidden registers: Sensationalism · Horror · Comedy · Sentimentality · Celebration · Brightness

---

## 5. Narration and Audio Targets

### Narration Model

- Voice-over only. Narration is always audio-only.
- Visuals support narration; they never depict it.
- When a narration beat references a person speaking, substitute with: the artefact they referenced, the environment where they spoke, or their physical action.

### Tone and Register

- Ancient authority. Archaeological gravitas. Measured mystery.
- The voice register of a serious documentary — not a sensationalist spectacle.
- Delivery: slow, deliberate, measured. Never frantic.

### ElevenLabs Voice Targets

- Voice ID: `j9jfwdrw7BRfcR43Qohk` (pinned 2026-06-04, irreversible)
- Stability: high (documentary consistency)
- No pitch variation for dramatic effect; let the script carry weight

### Audio Gate Checks

- Narration audio file must be present in final render.
- Duration mismatch between narration and video: WARN if > 10 s, FAIL if > 30 s.
- Captions/SRT file must be present and non-empty (FAIL if absent).

---

## 6. Subtitle Rules

### Format

- Provider: standard SRT or WebVTT (pipeline-generated from ElevenLabs transcript).
- Line length: max 42 characters per line.
- Max 2 lines per subtitle frame.
- Duration per card: 1.5–7 s.

### Style

- Font: Raleway Regular (when rendered in video player CSS).
- Color: parchment_cream (`#F5F3EE`).
- Background strip: semi-transparent dark (void_black at ~0.75 opacity) — never bright.
- Position: lower third, safe zone inside 10% margin.

### Content Rules

- Subtitles must faithfully reproduce narration verbatim.
- No paraphrasing. No editorial commentary in subtitle text.
- Speaker labels not required (voice-over only channel).

### Gate Check

- Captions asset must be present: FAIL if absent.
- Subtitle file must be non-empty: FAIL if zero bytes.

---

## 7. Title and Hook Rules

### Title Format

- All episode titles in Cinzel SemiBold rendering (all caps in display contexts).
- Max 60 characters for SEO compliance.
- No clickbait or sensationalist language.
- No fabrication or misrepresentation of historical fact.

### Hook Rules (First 30 seconds)

- Open with atmosphere, not with a question or teaser card.
- No "You won't believe…" / "This will shock you…" constructions.
- No countdown formats ("5 reasons…").
- Emotional register: awe and curiosity — not urgency or anxiety.

### YouTube Title Rules

- Include "The Hypogeum Chronicles" or "Hypogeum Chronicles" in the title or as channel context.
- Episode number format: `Ep. N —` prefix where applicable.
- Banned terms in titles and descriptions: see Section 8.

### Description Rules

- Description must be non-empty: FAIL if absent.
- Minimum recommended length: 150 words.
- Include source attribution for primary historical claims.
- Include chapter markers if video is > 10 minutes.
- Channel tagline "Unearthing the story beneath our feet" may appear in the description.

---

## 8. Disclosure and Attribution Rules

### Source Attribution

- All primary historical claims must be attributable to cited sources.
- Source attribution must appear in the video description.
- Format: author, title, publication year minimum.
- Do not present disputed interpretations as settled fact.

### AI Disclosure

- Videos produced with AI-assisted tools must not claim to be human-produced documentaries.
- AI generation methods need not be enumerated; a general disclaimer is sufficient.
- Suggested form: "Produced with AI-assisted research and visual generation tools."

### Copyright and Banned Terms

The following terms must not appear in titles, descriptions, or spoken narration:

**Conspiracy / fringe terminology:**
`conspiracy`, `cover-up`, `they don't want you to know`, `secret society` (as fact), `illuminati`, `reptilian`

**Sensationalist:**
`shocking`, `unbelievable`, `mindblowing`, `you won't believe`, `insane`, `crazy` (as descriptor for historical events)

**Fabricated authority:**
`scientists confirm`, `experts say` (without attribution), `proof that`, `definitive evidence`

**Copyright-risk phrases:** Do not reproduce verbatim text exceeding 4 sentences from any single copyrighted source without license.

### Music Licensing

- All background music must be licensed for YouTube commercial use (sync license or royalty-free with commercial tier).
- License metadata must be logged in the asset catalog.
- No uncleared samples.
