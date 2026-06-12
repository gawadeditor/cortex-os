# Brand Bible — Gap Report

**Generated:** 2026-06-06  
**Last updated:** 2026-06-08 (Gap #1 resolved — palette verified from approved Style Board)  
**Source:** `brand_bible_hypogeum.json` v1 (extracted from `bible.png`)  
**Purpose:** All fields requiring manual input, verification, or approval before `_global_status` can be set to `"active"`.

Fields are grouped by priority. **Blocking gaps** must be resolved before BGA enforcement activates. **Non-blocking gaps** can be resolved in parallel with production but must be complete before multi-platform publishing.

---

## Recorded Approval Decisions (2026-06-06)

The following decisions were made by the channel owner following the extraction audit. They are recorded here to prevent ambiguity about the current binding status of each section.

| Section | Decision | Status |
|---|---|---|
| `motion_language` | Remains advisory (`_binding: false`). All values inferred from static images. Not binding until explicitly defined. | Advisory |
| Inferred forbidden patterns (`composition_patterns`, `colour_patterns`, `motion_patterns`, `atmosphere_patterns`) | Advisory until explicitly approved. Cannot block the pipeline. | Advisory |
| Pre-existing policy items (forbidden visual modes, narration model, content patterns, Shorts overlay spec) | Remain binding only where already active elsewhere in the pipeline (VPD system prompt, Shorts spec). Not newly enforced by BGA until Bible is activated. | Conditional — active in pipeline, not yet via BGA |
| Directly observed rules (wordmark, typefaces, palette, graphic elements, thumbnail no-face rule) | Binding candidates. Will become active when `_global_status` is set to `"active"` following final human review. | Binding candidates |
| 24 `REQUIRES_HUMAN_APPROVAL` fields | Not binding. Must not be used by BGA as enforcement criteria. | Pending approval |

---

## Blocking Gaps

These fields are marked `_binding: true` in the Bible and are required for pipeline enforcement. The BGA cannot validate against a `TBD` value — it will skip the section and log `bible_section_unpopulated`.

---

### 1. Color System — Hex Value Verification — ✅ RESOLVED 2026-06-08

**Field path:** `color_system.primary_palette.*hex`  
**Resolution:** Channel owner provided the approved Style Board palette. All 7 values are human-verified and canonical. The initial 8-swatch image extraction is retired — `burnished_bronze (#7A5C14)` was a resolution artefact and is not present in the approved Style Board.

**Verified palette (canonical):**

| Swatch | Inferred Hex (retired) | Verified Hex | Status |
|---|---|---|---|
| void_black | `#080808` | `#080C0E` | **VERIFIED** |
| deep_stone | `#1A1520` | `#1A1D20` | **VERIFIED** |
| charcoal_earth | `#2B2020` | `#2B2E33` | **VERIFIED** |
| ancient_soil | `#4C3A20` | `#5C4A2F` | **VERIFIED** |
| antique_gold | `#B8882A` | `#C49A42` | **VERIFIED** |
| warm_gold | `#CBAA62` | `#E7D6B3` | **VERIFIED** |
| parchment_cream | `#F0E6C8` | `#F5F3EE` | **VERIFIED** |
| ~~burnished_bronze~~ | ~~`#7A5C14`~~ | ~~removed~~ | **RETIRED — not in Style Board** |

**Note:** BGA does not enforce specific hex values — enforcement remains character-based. The verified values are canonical reference for documentation and any future tooling.

---

### 2. Publishing Rules — AI Disclosure Text

**Field path:** `publishing_rules.ai_disclosure.required_text`  
**Current state:** `"TBD — exact disclosure text to be approved"`  
**Gap:** The binding disclosure text has not been set. The BGA enforces presence and formatting of the disclosure — it cannot validate correctness without the exact required string.

**Action required:** Draft the approved AI disclosure text. Insert it as the value of `required_text`. This text will appear verbatim at the end of every YouTube description without exception.

**Suggested starting point:** _(to be drafted by channel owner — must comply with platform AI content policies and any relevant jurisdiction requirements)_

---

### 3. Typography — Shorts Overlay Safe Zone Margin

**Field path:** `typography.shorts_overlay_type.safe_zone_margin`  
**Current state:** `"TBD — specify in pixels or percentage after format locked"`  
**Gap:** The Assembly Agent reads this field directly when positioning text overlays. Without a value, Assembly cannot place overlays within spec.

**Action required:** Define the safe zone margin as a percentage of frame height and width (e.g., `"8% horizontal, 12% vertical"`). This should account for platform UI chrome (subscribe button, captions, etc.) in 9:16 format.

---

### 4. Shorts Rules — Text Safe Zone Margin

**Field path:** `shorts_rules.text_safe_zone_margin`  
**Current state:** `"TBD"`  
**Gap:** Duplicate of the typography gap above but in the Shorts-specific section. Both fields must be set to the same value.

---

### 5. Publishing Rules — Title Max Characters

**Field path:** `publishing_rules.title_rules.max_characters`  
**Current state:** `"TBD"`  
**Gap:** The BGA validates title length in Pass 3. Without a max_characters value, it cannot enforce the limit.

**Action required:** Define the YouTube title character limit for this channel. YouTube's platform maximum is 100 characters. The channel standard may be more restrictive (e.g., 70 characters for full display without truncation in search results).

---

## Non-Blocking Gaps

These fields are either `_binding: false` or in sections with `_status: "partial"`. Production can continue without them, but they must be resolved before multi-platform publishing or before BGA is set to active.

---

### 6. Logo System — Minimum Size

**Field path:** `logo_system.primary_mark.minimum_size`  
**Current state:** `"TBD — specify after resolution testing"`  
**Gap:** Cannot enforce minimum logo scale without a defined value.

**Action required:** Test the wordmark at thumbnail scale (1280×720 reduced to ~100–120px wide in a feed view) and define the minimum rendered width in pixels at which HYPOGEUM remains legible.

---

### 7. Logo System — Clear Space

**Field path:** `logo_system.primary_mark.clear_space`  
**Current state:** `"TBD — minimum clear space around wordmark to be defined"`  
**Gap:** Without a clear space rule, the BGA cannot flag thumbnails or title cards where other elements encroach on the wordmark.

**Action required:** Define minimum clear space as a multiple of the cap height of HYPOGEUM (e.g., `"0.5× cap height on all sides"`).

---

### 8. Logo System — Watermark Rules

**Field path:** `logo_system.watermark_rules`  
**Current state:** `"TBD — lower third or corner placement to be defined"`  
**Gap:** Applies to main episode video output. Assembly Agent needs this for burn-in decisions.

**Action required:** Decide: (a) whether a watermark appears on main episode video, (b) placement (lower third left, lower right corner, etc.), (c) opacity, and (d) at what timecode it appears and disappears.

---

### 9. Logo System — End Card Rules

**Field path:** `logo_system.end_card_rules`  
**Current state:** `"TBD — end card layout to be defined"`  
**Gap:** Applies to the final seconds of every episode. Without a spec, the Assembly Agent cannot produce a compliant end card.

**Action required:** Define: (a) end card duration in seconds, (b) layout (wordmark position, subscribe element position, next video element), (c) background treatment (animated or static).

---

### 10. Social Media Rules — Instagram

**Field path:** `social_media_rules.instagram.story_format_rules` and `feed_format_rules`  
**Current state:** Both `"TBD"`  
**Gap:** No visual reference for Instagram formats was present in `bible.png`.

**Action required:** Define Instagram-specific visual rules for 9:16 story format and 4:5 or 1:1 feed format. Minimum: background treatment, wordmark usage, text treatment.  
**Note:** Not required until Instagram publishing begins.

---

### 11. Social Media Rules — TikTok

**Field path:** `social_media_rules.tiktok.caption_overlay_rules` and `format_rules`  
**Current state:** Both `"TBD"`  
**Gap:** No TikTok-specific visual reference in `bible.png`.

**Action required:** Define TikTok-specific overlay and format rules.  
**Note:** Not required until TikTok publishing begins.

---

### 12. Social Media Rules — YouTube End Card and Chapter Cards

**Field path:** `social_media_rules.youtube.end_card_rules` and `chapter_card_rules`  
**Current state:** Both `"TBD"`  
**Gap:** Covered partially by logo_system end card rules (gap 9 above), but the YouTube-specific variant needs to confirm platform formatting requirements.

**Action required:** Confirm YouTube end card platform requirements and align with the logo_system end card spec once gap 9 is resolved.

---

### 13. Color System — Text on Light Surfaces

**Field path:** `color_system.text_on_light`  
**Current state:** `"TBD — light surface usage not defined in reference. Avoid light surfaces."`  
**Gap:** The reference uses exclusively dark surfaces. No light surface spec was extractable.

**Action required:** Decide whether light surface use cases will ever arise (e.g., print materials, co-branded content, light-mode web). If yes, define text colors. If no, formally document that light surfaces are forbidden.

**Recommended resolution:** Mark light surfaces as forbidden in `forbidden_patterns.colour_patterns` and set `text_on_light: "forbidden — channel uses dark surfaces only"`.

---

### 14. Forbidden Patterns — Composition and Thumbnail Patterns Review

**Field paths:** `forbidden_patterns.composition_patterns`, `forbidden_patterns.thumbnail_patterns`  
**Current state:** Partially extracted from reference, but not exhaustive.  
**Gap:** These lists were inferred from the reference. They may be incomplete.

**Action required:** Review both lists and confirm they reflect every known binding prohibition. Add any missing items before activation.

---

### 15. Forbidden Patterns — Motion Patterns

**Field path:** `forbidden_patterns.motion_patterns`  
**Current state:** Extracted, but inferred from visual register — not from an explicit list in the reference.  
**Gap:** May be missing VPD-defined motion prohibitions that were established operationally.

**Action required:** Cross-reference with the VPD system prompt's motion rules and confirm the list is complete.

---

### 16. Texture System — Usage Rules

**Field path:** `texture_system.usage_rules` and `texture_system.style_rules`  
**Current state:** 6 texture categories observed and captured. Usage rules, opacity guidance, and layering rules are `REQUIRES_HUMAN_APPROVAL`.  
**Gap:** The TEXTURES panel in `bible.png` shows the material vocabulary but provides no specification of usage context.

**Action required:** Decide:
- Which textures apply to which output types (thumbnail backgrounds, title cards, chapter cards, video frame overlays, UI elements)
- Whether any textures are mandatory in specific contexts, or all decorative
- Application opacity range (e.g., "20–40% opacity overlay on video frames")
- Layering rules (e.g., "texture overlay sits below typography, above background fill")

---

## Summary

| Priority | Gap | Field Path | Blocking? |
|---|---|---|---|
| 1 | ~~Hex value verification (all 8 swatches)~~ — **RESOLVED** | `color_system.primary_palette.*hex` | ~~Yes~~ DONE |
| 2 | AI disclosure required text | `publishing_rules.ai_disclosure.required_text` | Yes |
| 3 | Shorts overlay safe zone margin | `typography.shorts_overlay_type.safe_zone_margin` | Yes |
| 4 | Shorts text safe zone margin | `shorts_rules.text_safe_zone_margin` | Yes |
| 5 | Title max characters | `publishing_rules.title_rules.max_characters` | Yes |
| 6 | Logo minimum size | `logo_system.primary_mark.minimum_size` | No |
| 7 | Logo clear space | `logo_system.primary_mark.clear_space` | No |
| 8 | Watermark rules | `logo_system.watermark_rules` | No |
| 9 | End card rules | `logo_system.end_card_rules` | No |
| 10 | Instagram rules | `social_media_rules.instagram.*` | No |
| 11 | TikTok rules | `social_media_rules.tiktok.*` | No |
| 12 | YouTube end card / chapter cards | `social_media_rules.youtube.end_card_rules` | No |
| 13 | Text on light surfaces | `color_system.text_on_light` | No |
| 14 | Forbidden composition/thumbnail patterns review | `forbidden_patterns.{composition,thumbnail}_patterns` | No |
| 15 | Forbidden motion/atmosphere patterns review | `forbidden_patterns.motion_patterns`, `atmosphere_patterns` | No |
| 16 | Texture system usage rules | `texture_system.usage_rules`, `texture_system.style_rules` | No |

**Blocking gaps to resolve before activation:** 4 (Gap #1 resolved 2026-06-08)  
**Non-blocking gaps:** 11  
**Total:** 16

---

## Not Gaps — Intentional TBDs

These fields are `"TBD"` by design and should not be resolved by populating values from this gap report:

- `shorts_rules.generation_model` — Set to `"disabled"`. Will be set to `"extracted"` when the Shorts extraction architecture (M6) is implemented. Do not change before M6.
- `_meta.approved_by` — Set when a human signs off on the populated Bible. Not a data-entry task.
- `_meta.bible_version` — Will increment from `"v1"` on next approved revision.
