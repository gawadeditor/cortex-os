# The Hypogeum Chronicles — Brand Bible

**Version:** v1.1  
**Status:** Extracted from approved visual reference (`bible.png`). Palette updated 2026-06-08: 8 inferred swatches replaced with 7 verified values from approved Style Board. **Inactive — not yet enforced.**  
**Audit:** Extraction audit completed 2026-06-06. See [`extraction_audit.md`](extraction_audit.md).  
**Source:** `docs/brand_guardian/bible.png`  
**Machine-readable:** `docs/brand_guardian/brand_bible_hypogeum.json`  
**Last updated:** 2026-06-08  
**Approved by:** _pending_

### Current Rule Status

| Category | Count | Notes |
|---|---|---|
| Directly observed, binding candidates | 23 | HIGH confidence, from bible.png |
| Inferred, advisory only | ~30 | motion_language, forbidden inferences |
| Pre-existing policy (already active) | 10 | VPD system prompt + channel policy |
| Requires human approval | 24 | Must not be treated as binding until approved |

**User decisions recorded 2026-06-06:**
- `motion_language` remains advisory (`_binding: false`) — all values inferred from static images
- Inferred forbidden patterns remain advisory until explicitly approved
- Pre-existing policy rules remain binding only where already active in the pipeline
- Directly observed rules are binding _candidates_ — not active until final human review sets `_global_status: "active"`

> The Brand Guardian Agent (BGA) enforces this Bible in the production pipeline. BGA is in advisory-only mode until `_global_status` is set to `"active"`. Before activation, complete all items in the [Gap Report](gap_report.md).

---

## Identity

**Channel name:** The Hypogeum Chronicles  
**Wordmark:** THE HYPOGEUM CHRONICLES  
**Tagline:** UNEARTHING THE STORY BENEATH OUR FEET

### Wordmark Structure

The wordmark is a three-line hierarchy. It is not a flat wordmark. The scale relationship between lines is mandatory.

```
THE                          ← small caps, wide tracking, subordinate
HYPOGEUM                     ← primary display, full weight, dominant scale
— CHRONICLES —               ← medium scale, em-dash flanking, equal weight to line 1
```

The em-dashes flanking CHRONICLES are a mandatory brand element — not decoration, not optional. Never substitute with hyphens. Never omit.

The tagline appears in Raleway Regular beneath the wordmark at a smaller scale. Wide tracking. Subordinate to the wordmark at all times.

### Visual Personality

Ancient authority. Archaeological gravitas. Measured mystery. The visual language of a serious excavation — not a sensationalist spectacle.

### Emotional Register

| Role | Register |
|---|---|
| Primary | Awe |
| Secondary | Curiosity · Unease · Reverence · Discovery |
| **Forbidden** | Sensationalism · Horror · Comedy · Sentimentality · Celebration · Brightness |

### Content Pillars

ARCHAEOLOGY — HISTORY — MYSTERY — DISCOVERY — HERITAGE — LEGACY

### Brand Positioning

Cinematic archaeology documentary. The visual register of prestige historical film — not social media content. The viewer expects depth, credibility, and atmosphere. Not entertainment froth.

**Brand promise:** Unearthing the story beneath our feet.

---

## Color System

Seven-swatch palette running from near-black to parchment cream. Warm earth and gold tones. No cool colors. No bright colors.

> Hex values verified 2026-06-08 from the approved Style Board. All 7 values are human-approved and canonical. The initial image extraction produced 8 values (all NOT_VERIFIED) — the approved Style Board collapses to 7 swatches (`burnished_bronze` removed as it was a resolution artefact).

### Full Palette

| Swatch | Hex | Role | Primary Use |
|---|---|---|---|
| void_black | `#080C0E` | Primary background | Base background for all primary surfaces, episode renders, title cards |
| deep_stone | `#1A1D20` | Secondary background | Background variant, shadow fills, panel backgrounds |
| charcoal_earth | `#2B2E33` | Surface / mid-depth | Card surfaces, secondary panel fills |
| ancient_soil | `#5C4A2F` | Warm dark accent | Border elements, separation lines, low-level accent fills |
| antique_gold | `#C49A42` | Primary accent | Icon fills, key text accents, decorative graphic elements, logo secondary color |
| warm_gold | `#E7D6B3` | Primary text accent | Primary accent text on dark backgrounds, logo principal color, headline highlight, graphic element outlines |
| parchment_cream | `#F5F3EE` | Primary light text | Body text on dark backgrounds, subtitle text, light text elements |

### Thumbnail Palette

Thumbnails may diverge from the primary palette for CTR purposes, but must remain within these bounds:

- **Background:** void_black or deep_stone (dominant)
- **Accent:** warm_gold or antique_gold
- **Text:** warm_gold (title) + parchment_cream (subtitle)
- **Contrast minimum:** WCAG AA. Gold text on near-black background.
- **Forbidden thumbnail colors:** bright white, pure red, neon (any hue), cool blue, green, purple, pink, bright orange

### Text Color Rules

- **Text on dark:** warm_gold (`#E7D6B3`) for display/titles. parchment_cream (`#F5F3EE`) for body.
- **Text on light:** Not defined in reference. Avoid light surfaces.
- **Video overlay opacity:** 0.70 to 0.92 for dark overlays on video frames.

### Globally Forbidden Colors

Bright white · Neon hues (any) · Cool blues · Greens · Pinks · Purples · Bright oranges

These apply to all outputs including period-specific palettes. A period-accurate palette may not use globally forbidden colors even if historically justified.

---

## Typography

Two-typeface system. No other typefaces permitted in any output.

> Font names extracted directly from the typography specimen in `bible.png`. Identification confidence is very high.

### Primary Typeface — Cinzel SemiBold

| Property | Value |
|---|---|
| Family | Cinzel |
| Weight | SemiBold |
| Classification | Classical serif — Roman inscriptional letterform |
| Availability | Google Fonts (free) |
| Case rule | ALL CAPS only. Cinzel is a caps-only design; lowercase renders as small caps. |
| Use case | All display text: channel name, episode titles, thumbnail headlines, section headers, graphic element labels |

### Secondary Typeface — Raleway Regular

| Property | Value |
|---|---|
| Family | Raleway |
| Weight | Regular |
| Classification | Geometric sans-serif |
| Availability | Google Fonts (free) |
| Case rule | Sentence case for body copy. ALL CAPS with wide tracking for navigation/label uses. |
| Use case | All body text: subtitles, taglines, captions, Shorts overlays, metadata copy, supporting text |

### Thumbnail Typography Rules

| Rule | Value |
|---|---|
| Primary font | Cinzel SemiBold |
| Secondary font | Raleway Regular |
| Headline max words | 4 |
| Headline max characters | 30 |
| Headline case | ALL CAPS |
| Headline tracking | Wide |
| Subtitle max words | 7 |

### Shorts Overlay Typography

- Font: Raleway Regular (not Bold)
- Max words per overlay: 7 (hard limit)
- Case: ALL CAPS
- Safe zone margin: TBD

### Forbidden Typefaces

Any typeface not in the two-font system is forbidden. Specific examples: Comic Sans, Impact, Arial, Helvetica (as display), script/handwriting fonts.

### Forbidden Treatments

Bold Raleway in overlays · Lowercase Cinzel (small caps only) · Mixed case in display text · Drop shadows on text · Outlined text strokes · Gradient fills on text

---

## Thumbnail Language

### Composition Model

**Wordmark-dominant.** The channel identity is the primary compositional anchor. Episode-specific imagery is background context — not foreground subject.

### Required Elements

1. Channel wordmark (THE HYPOGEUM CHRONICLES) with full three-line hierarchical treatment
2. Background: atmospheric stone, architectural, or environmental image — dark and charged
3. Episode-specific title or subtitle in Raleway beneath the wordmark _(optional — present in 3 of 4 reference examples)_

### Face Rule

**No human faces in thumbnails.** The brand uses environmental, architectural, and artefact imagery — not portraiture. None of the four reference examples contain human faces.

### Thumbnail Variants

Four confirmed variants from the reference:

| Variant | Structure |
|---|---|
| Tagline | Wordmark + tagline "UNEARTHING THE STORY BENEATH OUR FEET" |
| Episode subtitle | Wordmark + "EPISODE N: [EPISODE TITLE]" in Raleway |
| Statement | Wordmark + short declarative statement ("THE PAST IS NOT SILENT") |
| Wordmark only | Wordmark alone, strongest atmospheric background |

### Background Treatment

Full-bleed atmospheric image. Dominant tone: void_black or deep_stone. Subject matter: stone ruins, underground architecture, maps, carvings, arched corridors. Slight vignette or darkening at edges to anchor the wordmark.

Brightness: 0–40% on background. Wordmark text: warm gold at high contrast.

### Approval Threshold

Wordmark legible at thumbnail scale. Background atmospherically on-brand. No forbidden elements present.

### Forbidden Compositions

- Human face as primary subject
- Bright or high-key backgrounds
- Horizontal text layout that disrupts wordmark hierarchy
- Subtitle text that dominates over HYPOGEUM scale
- Any composition that buries or reduces the channel wordmark

---

## Documentary Visual Grammar

**Status: Active.** These rules are already enforced in the VPD system prompt independent of the BGA. The BGA formalises enforcement — it does not create new rules.

### Default Visual Modes

These are the on-register modes for all scene planning:

- Environmental
- Architectural
- Artefact detail
- Historical reconstruction (action-based — never speech-based)
- Observational
- Atmospheric establishing
- Underground chamber
- Stone surface closeup
- Cartographic overhead
- Single figure silhouette in environment

The visual reference confirms all of these: underground chambers with light shafts, stone carvings, mountainous landscapes, antique maps, arched corridors, single human silhouette against architectural scale.

### Forbidden Visual Modes — Absolute Prohibition

These modes are permanently forbidden. **No agent, prompt, config flag, or narrative justification can authorize them.**

- Talking head
- Interview framing
- Visible lip sync
- Direct address to camera
- Mouth movement synchronized with narration
- Characters visibly speaking under narration

### Motion Rule

All figures derive motion from **physical action** — not from speech or facial expression. A figure may walk, lift, chisel, excavate, gesture to an object. A figure may not speak to camera, narrate, or mouth words.

### Narration Model

Voice-over only. Narration is always audio-only. Visuals support narration; they never depict it.

When a narration beat references a person speaking, substitute with:
- The artefact they referenced
- The environment where they spoke
- Their physical action

Document as CAPABILITY_LIMITED only if the narrative intent is materially lost.

### Lighting Grammar

| Property | Rule |
|---|---|
| Primary source | Single strong directional source — shaft of light, torch, candle, ceiling opening |
| Character | Warm amber or cool natural daylight. Never flat studio lighting. |
| Shadow depth | Deep, rich shadows. ~70–90% of frame in shadow on establishing shots. |
| Highlight ratio | High contrast. Light source is the story element. Darkness is context. |
| **Forbidden** | Flat even lighting · Overexposed bright scenes · Neon colored lighting · Ring light aesthetics |

---

## Motion Language

### Speed Register

**Slow. Deliberate.** All camera movement is measured and purposeful. Nothing is frantic.

### On-Register Motion Types

slow_pan · slow_push_in · slow_pull_out · static_with_atmosphere · slow_aerial_drift · slow_tracking_along_surface · environmental_motion_only

### Off-Register Motion Types

fast_cut_whip_pan · handheld_shake · zoom_burst · speed_ramp · jitter_or_vibration · social_media_snap_cut

### Preferred Motion by Scene Type

| Scene Type | Preferred Motion |
|---|---|
| Establishing | slow_push_in or slow_pan — reveal the space gradually |
| Detail shot | static or very slow_push_in — hold on the artefact or surface |
| Revelation | slow_push_in toward key subject — the camera acknowledges significance |
| Transition | static cut or slow_pull_out — exit as slowly as entry |
| Atmospheric | environmental_motion_only — let the world move, not the camera |
| Underground chamber | slow_pan revealing architecture — single light source as focal anchor |

### Forbidden Motion Patterns

Rapid cuts or montage sequences · Camera shake or instability · Speed variations · Whip pans · Zoom bursts · Social-media style snap cuts · Character-driven POV (unless first-person archaeological discovery moment)

---

## Logo System

### Primary Mark

**Wordmark:** THE HYPOGEUM CHRONICLES in Cinzel SemiBold  
**Icon:** Architectural columnar/temple icon — small, centred between HYPOGEUM and CHRONICLES in full treatment  
**Icon style:** Fine line art, minimal, archaeological  
**Primary color:** warm_gold (`#E7D6B3`) on dark backgrounds

### Permitted Placements

- Thumbnail: centred
- End card: centred
- Lower third: left-anchored

### Color Variants

1. warm_gold (`#E7D6B3`) on void_black (`#080C0E`) (primary)
2. parchment_cream (`#F5F3EE`) on void_black (`#080C0E`) (alternate)
3. warm_gold (`#E7D6B3`) on deep_stone (`#1A1D20`) (secondary surface)

### Em-Dash Rule

Em-dashes flanking CHRONICLES (`— CHRONICLES —`) are mandatory wordmark elements. Never omit. Never substitute with hyphens or dashes of any other width.

### Forbidden Treatments

Rotating or distorting the wordmark · Substituting a different font · Removing the em-dash decorators · Using the wordmark in bright or saturated colors · Placing the wordmark on a light background · Scaling CHRONICLES larger than HYPOGEUM

---

## Iconography

Five graphic elements confirmed from the visual reference. All are fine line art in warm_gold on dark backgrounds.

| Element | Description | Usage |
|---|---|---|
| Compass rose | Multi-point wind rose / navigation compass. Eight or sixteen directional points. | Section dividers, loading states, chapter cards |
| Ouroboros | Snake consuming its own tail. Ancient symbol of cyclicality. | Episode end cards, identity motif |
| Classical columns | Column section — capital and shaft detail. Classical Greco-Roman reference. | Section headers, heritage thematic references |
| Drafting compass | Technical drawing compass/dividers. Archaeological survey reference. | Discovery/measurement thematic references, cartographic sections |
| Circle medallion | Simple geometric circle. Minimal. | Framing device, section marks, minimal decorative use |

### Icon Style Rules

- **Rendering:** Fine line art only. No fills other than the line itself.
- **Color:** warm_gold (`#E7D6B3`) on dark backgrounds only
- **Weight:** Consistent thin-to-medium stroke weight across all five elements
- **Background:** Always on void_black (`#080C0E`) or deep_stone (`#1A1D20`) — never on light backgrounds

### Forbidden Icon Treatments

Filled solid icons · Drop shadows · 3D rendering · Color fills other than warm_gold/antique_gold · Icons on light backgrounds

### Symbolic Vocabulary

**Preferred:** Navigation and cartography · Archaeological tools and instruments · Classical architecture · Ancient symbols · Measurement and precision instruments

**Forbidden:** Modern technology references · Contemporary iconography (social media icons, app-style icons) · Supernatural or occult symbols · Tribal or culturally appropriative symbols · Emoji

---

## Texture System

**Status:** Observed — usage rules require human approval. `_binding: false` until usage is defined.

The TEXTURES panel in `bible.png` shows 6 square swatches, 2 rows of 3, running dark to light. These establish the material vocabulary of the channel's visual language — the surfaces against which all content is placed.

### Observed Texture Categories

| Name | Description | Confidence |
|---|---|---|
| black_stone_obsidian | Near-black smooth stone surface. Minimal variation. Consistent with void_black palette role. | HIGH |
| carved_stone_relief | Stone surface with carved or incised markings. Ancient inscribed surfaces — consistent with visual style board. | MEDIUM |
| archaeological_surface_detail | Worn, eroded stone. The material of dig sites and ruins. | MEDIUM |
| dark_map_line_texture | Darker surface with fine linear detail. Map or aged document texture. | MEDIUM |
| cracked_earth_aged_plaster | Medium-tone surface with cracking or desiccation pattern. Ancient walls and floors. | MEDIUM |
| parchment_aged_paper | Lightest swatch. Warm cream-to-tan, aged paper quality. Consistent with parchment_cream palette role. | HIGH |

### What is confirmed

- All 6 textures are warm earth tones — fully consistent with the primary palette (void_black through parchment_cream).
- The texture vocabulary is entirely archaeological and historical. No modern materials, no synthetic surfaces.

### What requires human approval

> The image shows the texture types but does not specify where or how they are applied.

- Which textures apply to which production contexts (title cards, chapter cards, thumbnail backgrounds, UI)
- Layering rules (how textures overlay video frames or branded surfaces)
- Application opacity
- Whether textures are mandatory or decorative in specific contexts

---

## Forbidden Patterns

Exhaustive list. **Add to this list; never remove.** No agent, prompt, config flag, or narrative justification can authorize any item on this list.

### Visual Patterns

Talking heads · Visible lip sync · Interview framing · Characters speaking to camera · Mouth movement as primary motion

### Composition Patterns

Human faces as primary thumbnail subject · Bright high-key backgrounds in any branded material · Horizontal layouts that destroy the wordmark hierarchy · Any composition that minimises or subordinates the channel wordmark in thumbnails

### Content Patterns

Fabricated historical footage presented as authentic · Synthesized voice or likeness of named historical or living individuals · Disputed interpretation presented as settled historical fact

### Thumbnail Patterns

Human faces as primary subject · Bright or saturated color palette · Non-Cinzel/Raleway typography · Missing em-dash decorators on CHRONICLES · Episode imagery dominating over channel wordmark · Light or neutral backgrounds

### Typography Patterns

Any typeface outside Cinzel SemiBold and Raleway Regular · Bold Raleway in overlays · Mixed case in Cinzel display text · Drop shadows on text · Outlined text strokes · Gradient fills on text · Decorative or script typefaces

### Colour Patterns

Bright white (`#FFFFFF`) as any surface · Pure red · Neon hues (any) · Cool blues as primary color · Greens · Pinks or purples · Bright or warm oranges outside the burnished_bronze range

### Motion Patterns

Rapid cuts or montage sequences · Camera shake or handheld instability · Speed ramps or velocity variations · Whip pans · Zoom bursts · Social-media style snap cuts

### Atmosphere Patterns

Comic or lighthearted register · Bright, cheerful, or optimistic visual treatment · Flat even lighting (studio look) · Overexposed or blown-out highlights · Neon or synthetic colored lighting

---

## Shorts Rules

**Aspect ratio:** 9:16  
**Generation model:** disabled (`ENABLE_DEDICATED_SHORT_GENERATION=false`)  
**Typography:** Raleway Regular for all text overlays. Cinzel SemiBold for hook text only if used.  
**Text color:** warm_gold or parchment_cream on dark video frame  
**Text safe zone margin:** TBD

### Overlay Structure

Three required beats per Short:

1. **Opening hook** — create the curiosity gap
2. **Key information** — the archaeological or historical anchor
3. **Payoff / curiosity** — the unresolved hook driving to the full episode

**Max words per overlay:** 7 (hard limit, non-negotiable)  
**Overlay positions:** top_third · bottom_third · top · bottom

**Redirect text rule:** Silent text overlay only. Maximum 7 words. Drive to full episode. Not promotional copy.

### Shorts Background

All background imagery follows the same documentary visual grammar as the main episode. No bright backgrounds. No talking heads.

### Shorts Forbidden Patterns

Generic engagement bait text · Episode central answer revealed · Bright or saturated backgrounds · Human faces as primary subject · Talking head format

---

## Social Media Rules

**Status: Partial.** YouTube rules extracted from reference. Instagram and TikTok platform-specific formatting is TBD.

### YouTube

- **Thumbnail:** See Thumbnail Language section. Wordmark-dominant. Atmospheric dark background. No faces.
- **Title card:** Cinzel SemiBold for title. Raleway Regular for episode number/subtitle. Gold on dark.
- **End card:** TBD
- **Chapter cards:** TBD

### Instagram & TikTok

TBD — platform-specific rules not in the visual reference. To be defined before multi-platform publishing begins.

---

## Publishing Rules

### AI Disclosure

- **Required:** Yes, in every published piece without exception.
- **Placement:** End of YouTube description
- **Required text:** TBD — exact disclosure text must be approved before activation
- **Forbidden to omit:** True — even if the disclosure increases description length beyond optimal, it cannot be removed.

### Title Rules

- **Preferred structure:** [Episode Title] | The Hypogeum Chronicles
- **Max characters:** TBD
- **Forbidden:** Clickbait superlatives · ALL CAPS episode title · Emoji in title · Numbers as primary hook (Top 10...)
- **Required:** Episode-specific title that reflects the curiosity gap

### Description Rules

**Required sections:** logline · chapters · sources · ai_disclosure  
**Forbidden:** Generic SEO stuffing · Off-brand promotional language · Claims not supported by the research package

### Tag Rules

**Required categories:** history · archaeology · mystery · documentary  
**Forbidden:** Unrelated viral tags · Celebrity or person names not featured in the episode

---

## Activation Checklist

Before setting `_global_status: "active"` in `brand_bible_hypogeum.json`:

- [ ] Hex values verified by eyedropping from original `bible.png` source file
- [ ] AI disclosure exact required text approved and inserted
- [ ] Title rules `max_characters` defined
- [ ] Shorts overlay `safe_zone_margin` defined
- [ ] Logo `minimum_size` and `clear_space` defined
- [ ] Watermark rules defined
- [ ] End card rules defined
- [ ] Instagram and TikTok platform rules defined
- [ ] `forbidden_patterns.composition_patterns`, `thumbnail_patterns`, `motion_patterns` reviewed and finalised
- [ ] `publishing_rules.title_rules.forbidden_patterns` reviewed and finalised
- [ ] BGA agent system prompt written and tested (`hypogeum/prompts/brand_guardian_agent.txt`)
- [ ] BGA schema added to `hypogeum/schemas.py`
- [ ] BGA integrated into VPD, TD, and pipeline
- [ ] At least one full episode run with BGA in advisory mode before activation

See full gap detail: [gap_report.md](gap_report.md)
