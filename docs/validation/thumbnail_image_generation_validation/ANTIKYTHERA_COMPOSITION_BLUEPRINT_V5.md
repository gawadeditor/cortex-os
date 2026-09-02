# Antikythera Thumbnail — Composition Blueprint v5

**Status:** Design only. No provider call made. V3 and V4 preserved on disk,
not deleted. V5 exists to correct a real visual regression identified in
professional review of the V4 production-route generation
(`candidate_1.jpg`, `.hypogeum-assets/episodes/val_thumbnail_image_gen/
ep_003_v4_production_route/`).

---

## 1. Root cause of the V4 visual regression

The runtime wiring (Thumbnail DNA v1) worked correctly — all four prompt
components were present exactly once, in order. The regression was a
**content gap in the DNA schema itself**, not a plumbing bug:

1. **No structured field constrained the hero/tertiary object's size.**
   `composition_plan` had no `hero_object_max_frame_share_pct` at all. The
   V4 topic-specific prompt therefore hardcoded "roughly 35-40% of frame"
   for the mechanism independently, with nothing in the DNA directive able
   to contradict or cap it — the DNA directive's own hero-object language
   was purely qualitative ("clear and readable at a glance"), no ceiling.

2. **The "impossibility" trigger's `eye_path` never included a human
   figure**, yet `describe_dna_constraints()`'s own prose already said
   "never erases the readability of the human figure" — a pre-existing
   internal inconsistency. With no `human_figure` step in `eye_path`,
   `brightest_source_role` derived to `"hook_text"`, not the doorway/figure
   — so the DNA directive never actually reinforced the figure as the
   intended secondary read, and nothing constrained its gesture or mobile
   readability. The topic-specific prompt's "active discovery gesture"
   language was carrying the entire weight of that requirement alone,
   un-reinforced by anything else in the prompt.

3. **Typography had no explicit per-line ceiling.** `_embed_typography_in_
   prompt()` states `EXIST` is "~2× taller" than the headline with no cap
   tied to `headline_zone_width_pct`. Combined with the topic prompt's own
   "monumental" language for the headline (with no matching qualifier on
   `EXIST` specifically), two independent instruction sources both pushed
   `EXIST` toward maximum scale with nothing capping it against the header
   zone's own declared width.

4. **No field constrained patina prominence.** "Dark verdigris-bronze
   patina" was mentioned once, in prose, with no cap — and the brand
   directive's forbidden-color rule only excludes *bright turquoise*
   verdigris, so a moderate/dark green reads as fully compliant while still
   being more visually prominent than intended.

5. **Positional/repetition asymmetry compounded all of the above.** The DNA
   directive is component 3 of 4 — a single paragraph appended after three
   long, specific, topic-prompt paragraphs (headline, doorway/figure,
   mechanism). Where the DNA directive had no matching number to reinforce
   (mechanism size, patina cap, figure requirement), the earlier, more
   detailed, more specific topic-prompt language dominated in practice.

**Net effect:** wiring was never the problem. The DNA schema was missing
exactly the fields needed to (a) give the topic-specific prompt numbers to
derive from instead of inventing its own, and (b) let the DNA directive
state the *same* numbers as reinforcement rather than vague qualitative
language with no matching figure.

---

## 2. Prompt contradictions found (as asked, itemized)

| Check | Finding |
|---|---|
| Mechanism described as 35–40% of frame, overpowering DNA | **Yes** — no `hero_object_max_frame_share_pct` field existed; the topic prompt's number was the only number in the entire prompt for hero-object size |
| `EXIST` "larger" language conflicts with headline ceiling | **Yes** — `headline_visual_weight_ceiling_pct` existed but nothing tied the *emphasis line specifically* to a width number; `_embed_typography_in_prompt()`'s "~2× taller" instruction had no counterweight |
| "Headline first" + "monumental" overpowers controlled-scale rule | **Partially** — "monumental" is applied to the headline generally in the topic prompt without a paired "controlled" qualifier at the same location; the DNA directive's "monumental but controlled" appears only once, late, generically |
| Mechanism description contains too much detail language | **Yes** — "a few large gear forms and one dial partially caught by rim light" plus "warm gold rim lighting on only the nearest edges" gave the mechanism its own dedicated light source and multiple descriptive clauses, rivaling the doorway |
| Active human gesture too weak vs. object/typography instructions | **Yes** — figure got one clause in one paragraph; headline got two (topic prompt + `_embed_typography_in_prompt` block); mechanism got its own full paragraph plus a rim-light clause |
| Green/verdigris conflicts with muted-bronze emphasis | **Yes** — no prominence cap existed; brand directive only forbids *bright* turquoise, not moderate green |
| DNA appended too late, lower practical weight | **Yes** — confirmed structurally (see root cause #5) |

---

## 3. Structured controls added or reused (Thumbnail DNA v2)

Added to `hypogeum/agents/thumbnail_composition_planner.py` (topic-agnostic,
defaults applied to every trigger; `"impossibility"` carries the tightened
V5 override values, not a special case — see module docstring for the
general rationale):

| Field | Type | Generic default | `"impossibility"` override |
|---|---|---|---|
| `hero_object_max_frame_share_pct` | `[lo, hi]` | `[25, 35]` | `[28, 32]` |
| `hero_object_concealment` | enum | `"partially_concealed"` | `"partially_concealed"` |
| `hero_object_detail_density` *(reused, DNA v1)* | enum | `"moderate"` | `"reduced"` |
| `headline_emphasis_max_width_pct` | derived = `headline_zone_width_pct[1]` | `38` | `34` |
| `focal_priority` | derived dict from `eye_path[0:3]` | — | `{primary: hook_text, secondary: human_figure, tertiary: focal_object}` |
| `human_figure_readable_at_mobile` | derived | `"not_applicable"` | `"required"` |
| `human_figure_gesture` | derived | `"none"` | `"active"` |
| `patina_prominence_max` | enum | `"subtle"` | `"subtle"` (already matches — no override needed) |
| `headline_zone_width_pct` *(reused, DNA v1)* | `[lo, hi]` | `[30, 38]` | `[30, 34]` |

`focal_priority` and `headline_emphasis_max_width_pct` are **derived**, not
independently settable — `eye_path` and `headline_zone_width_pct` remain
the single sources of truth (no duplicate fields introduced).

**Root-cause structural fix:** `"impossibility"`'s `eye_path` was corrected
from `["focal_object", "hook_text", "wordmark"]` (no human figure at all)
to `["hook_text", "human_figure", "focal_object", "wordmark"]`. This is a
general refinement of the trigger — a documentary "impossible object"
concept benefits from a human-scale discovery figure by default (the Brand
Bible's own `documentary_visual_grammar` lists
`single_figure_silhouette_in_environment` as on-register, rule E-VG02) —
not an Antikythera-only patch.

---

## 4. Topic-specific prompt now derived from composition plan

**Yes.** The V5 topic-specific prompt below uses the exact same numbers as
`composition_plan` and `describe_dna_constraints()` output — `30-34%` for
the headline zone, `34%` for the `EXIST` line cap, `28-32%` for the
mechanism, `"partially concealed"` / `"reduced detail"` / `"subtle"` patina
language — copied from the live plan values computed above, not
independently authored. There is no number in the topic-specific prompt
that does not also appear in the DNA directive.

---

## 5. V5 composition plan (produced by the canonical system, zero special-case code)

```json
{
  "headline_zone_width_pct": [30, 34],
  "headline_emphasis_max_width_pct": 34,
  "headline_visual_weight_ceiling_pct": 40,
  "hero_object_max_frame_share_pct": [28, 32],
  "hero_object_concealment": "partially_concealed",
  "hero_object_detail_density": "reduced",
  "patina_prominence_max": "subtle",
  "focal_zone_buffer": "required",
  "highlight_containment": "required",
  "mobile_readability_target": "320x180",
  "focal_zone": "center_right",
  "focal_scale": "medium",
  "text_zone": "lower_left",
  "eye_path": ["hook_text", "human_figure", "focal_object", "wordmark"],
  "focal_priority": {"primary": "hook_text", "secondary": "human_figure", "tertiary": "focal_object"},
  "contrast_mechanism": "human_vs_artifact",
  "brightest_source_role": "human_figure",
  "human_figure_gesture": "active",
  "human_figure_readable_at_mobile": "required",
  "typography_integration": "embedded"
}
```

Typography plan (unchanged mechanics — `plan_typography("IT SHOULDN'T EXIST", "impossibility", comp_plan)`):
headline=`"IT SHOULDN'T"`, emphasis=`"EXIST"`, `emphasis_scale=1.7`, zone=`"lower_left"`, alignment=`"left"`.

---

## 6. Exact V5 integrated provider prompt

Topic-specific concept text (component 1) — every zone number below is
copied verbatim from the V5 composition plan above:

```
Premium archaeological investigative documentary YouTube thumbnail, 16:9 — a serious archaeological-mystery editorial poster, not a fantasy or adventure-game illustration. One complete composition with a strictly controlled eye path: the headline is the primary read, the human discovery moment is the secondary read, the mechanism is the tertiary read — never let the tertiary object compete with the primary or secondary read.

HEADLINE ZONE (left 30-34% of frame, controlled not consuming): render the exact two-line headline:

IT SHOULDN'T
EXIST

Both lines left-aligned, stacked with clear breathing room between them. "IT SHOULDN'T" is condensed serif lettering, restrained scale. "EXIST" is larger but is capped at the same 30-34% zone width — it must not expand past the declared headline zone, must not touch the bottom frame edge, and must not visually outweigh the doorway/figure moment beside it. The letterforms are carved weathered stone with a muted stone-and-bronze base surface — NOT solid bright gold, not a glowing or dominant gold mass. Cracked, distressed texture. Subtle three-dimensional bevel. A restrained warm-gold highlight catches only the upper-left edge of each letter. Dark inner shadow. Restrained black drop shadow. Perfectly spelled, highly legible at 320x180, physically integrated into the scene's lighting.

HUMAN DISCOVERY ZONE — SECONDARY READ (center-left, offset left of the vertical midline, not centered): a glowing ancient stone doorway casts localized warm amber light down worn stone steps — the light is contained, does not blow out, and stays subordinate in brightness to the headline. A single human figure is the clear secondary read of this composition: caught mid-step, one hand actively reaching toward the light as if just discovering it — a visibly active gesture, not a passive standing pose. The figure is sized and positioned to remain clearly identifiable and readable even at 320x180 — it must read instantly as a person in motion, not a small or ambiguous silhouette. Backlit, faceless, creating scale and narrative pull. This is the second thing the eye finds, before the mechanism.

MECHANISM ZONE — TERTIARY READ, STRICTLY CONTAINED (right foreground, occupying at most 28-32% of the frame, partially cropped by the right frame edge): a corroded bronze Antikythera-like mechanism, mostly in shadow and only partially revealed — a glimpse, not a full display. Only a few large, simple gear forms are visible; the rest recedes into near-black. No dense mechanical detail, no field of small gears, no full illumination. The bronze is primarily dark and aged; any verdigris patina is subtle and muted, never a dominant green color field, never brighter or more saturated than the bronze itself. A thin warm gold rim light touches only the nearest edge — the mechanism must remain visibly the dimmest, least detailed, least busy of the three zones, clearly subordinate to both the headline and the human discovery moment.

Strong near-black negative-space separation between all three zones — deep shadow gaps keep the headline, the human discovery moment, and the mechanism visually distinct, never fused into one block. Asymmetric composition, not a balanced rule-of-thirds grid. One coherent camera perspective, one physically continuous underground chamber, one warm directional light source anchored at the doorway. Restrained, investigative mood — mystery and discovery, not spectacle or grandeur.

No extra text, no random lettering, no inscriptions, no symbols, no watermarks.
```

**Canonical DNA directive** (component 3, from `describe_dna_constraints(comp_plan)`, live output):

```
 — THUMBNAIL COMPOSITION DNA: Deliberate visual hierarchy with a clear eye path from primary to secondary to tertiary read. Explicit focal priority (primary read = hook_text; secondary read = human_figure; tertiary read = focal_object). The headline occupies roughly 30-34% of frame width, and its single largest/emphasis line must not exceed 34% of frame width either — monumental but controlled, not consuming the composition. The human figure's pose is visibly active — mid-motion, reaching, stepping, or otherwise engaged, never a static passive stance. The figure must remain clearly readable at 320x180. Strong near-black negative-space separation between every focal zone — no crowding, no fused blocks. The brightest light source stays localized and contained, never a full-frame wash, and never erases the readability of the human figure or hero subject near it. The hero/mystery/tertiary object occupies at most roughly 28-32% of the frame, is mostly in shadow, only partially revealed — a glimpse, not a full display, and is simplified to a few large, instantly legible forms, not a dense detailed display. Any weathering, patina, or corrosion coloring on that object is subtle and secondary to the material's dark aged-bronze base tone — never a dominant color field. Composition must remain clearly readable at 320x180.
```

**Canonical FULL brand directive** (component 4, unchanged from V4, brand spec `v1.1`):

```
 — HYPOGEUM BRAND VISUAL IDENTITY (thumbnail, full enforcement): Palette: near-black #080C0E and charcoal #1A1D20 dominate (60-70% of frame); dark stone #2B2E33 and muted bronze #5C4A2F as midground/structural material (20-30%); aged gold #C49A42 as a restrained accent only (5-10%, never a dominant field color); warm ivory #E7D6B3 and off-white #F5F3EE for highlights and text only, never a background or large fill. Forbidden: blue or cyan lighting of any kind; teal lighting; turquoise verdigris (patina rendered as bright cyan-green rather than muted dark bronze-green); orange/yellow scene wash covering the full frame; full-frame sepia tint; bright polished gold (flat, high-value, chrome-like gold with no aging/texture); fluorescent or neon colors of any hue; heavy saturated color cast dominating the overall frame. Finish: premium archaeological documentary, near-black dominant, restrained aged gold, weathered stone and bronze, realistic photographic texture, controlled directional light, deep but readable shadows. Avoid: generic fantasy-art finish, museum-display finish, glossy modern product-render look.
```

Component 2 (integrated typography instructions) is appended by
`_embed_typography_in_prompt()` from the typography plan above, unchanged
mechanically from V4.

No provider call has been made against this prompt. Generation is deferred
pending explicit approval, per instruction.
