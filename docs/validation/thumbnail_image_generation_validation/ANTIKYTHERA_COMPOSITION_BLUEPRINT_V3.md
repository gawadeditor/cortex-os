# Antikythera Thumbnail — Composition Blueprint v3

**Status:** Design only. No provider call made. Supersedes the v2 composition
(`ep003_tsc_A_hybrid_v2.jpg`) and the v1 integrated-text composition
(`ep003_tsc_A_integrated_text01.jpg`), both of which are preserved on disk,
not deleted.

**Why v3:** v1 (integrated-text) confirmed GPT Image 2 can render exact
headline text baked into the image. But the composition itself has six
problems (per user review, 2026-07-13):

| # | Problem in v1 | Fix in v3 |
|---|---|---|
| 1 | Headline, doorway, mechanism read as three competing blocks | Explicit hierarchy + diagonal eye path forces a read order |
| 2 | Doorway too centrally/predictably placed | Doorway shifted center-left, off the vertical midline |
| 3 | Mechanism too visually dense, competes with headline | Mechanism simplified to fewer, larger gear forms — legible at a glance, not a gear-texture wall |
| 4 | Figure too small for a strong narrative hook | Figure enlarged, positioned directly at/before the light threshold |
| 5 | Reads as fantasy poster, not investigative documentary | Visual-finish directive (no fantasy-art, no museum-display, no glossy render) now injected automatically via `hypogeum.brand.visual_identity`; prompt opening line explicitly frames the piece as investigative documentary, not adventure/fantasy poster |
| 6 | Gold occupies too much visual attention | Headline treatment shifted to muted stone/bronze with restrained `#C49A42` highlights only, not solid bright gold letterforms |

**Revision note (2026-07-13, same-day refinement):** Reviewed against a second, more specific
problem list: mechanism too detailed/dominant, figure not narratively strong enough, composition
still reading as fantasy grandeur rather than mystery. Two targeted changes made below (not a new
file — this blueprint is updated in place to remain the single canonical composition doc):
1. Mechanism zone rewritten from "simplified but fully visible" to "partially concealed in shadow" —
   the mechanism is now a partial glimpse, not a fully-exposed detailed object, to read as
   investigative mystery rather than fantasy spectacle.
2. Figure zone rewritten to give the figure an active discovery gesture (reaching toward / stepping
   into the light) rather than a passive standing pose, strengthening it as the narrative hook.

---

## Required hierarchy (read order)

1. **Headline** — first read
2. **Human discovery moment** (figure + light) — second read
3. **Mechanism** — third read, the mystery reveal

## Framing ratios (compositional guides, not hard crops)

| Zone | Share of frame | Content |
|---|---|---|
| Headline zone | left 36–40% | Two-line headline, muted stone/bronze |
| Doorway + figure | ~22–25% | Center-left, off-axis; figure large and clear |
| Mechanism reveal | ~35–40% | Right foreground, partially cropped, simplified |

## Eye path

Diagonal: **headline (left, upper-mid)** → **figure/light (center-left, lower-mid)** →
**mechanism (right, foreground)**. Deliberately asymmetric — not a balanced
rule-of-thirds grid. Near-black negative space separates all three focal
elements so none visually fuse into the next.

## Typography direction (refined from v1)

- Headline size reduced ~10–15% versus v1
- More vertical breathing room between `IT SHOULDN'T` and `EXIST`
- `EXIST` remains larger but stays clear of the bottom frame edge
- Letterforms: muted stone/bronze base with **restrained** `#C49A42` highlights
  only — not solid bright gold. This is the single biggest correction: v1's
  letters read as a dominant gold mass; v3's letters read as carved stone
  that happens to catch gold light, consistent with `color_system.usage_ratios`
  (`aged_gold` capped at 5–10% of frame, "never a dominant field color").
- Monumental condensed serif, cracked/weathered texture, subtle bevel,
  restrained highlight only along upper-left edges, dark inner shadow,
  black drop shadow — retained from v1, still legible at 320×180.

---

## Revised full integrated-image prompt

This is the composition text only. The canonical brand directive below it
is appended programmatically by `hypogeum.brand.visual_identity.build_visual_identity_directive("thumbnail")`
at generation time — reproduced here verbatim (not retyped) so this document
reflects exactly what the code will send.

```
Premium archaeological investigative documentary YouTube thumbnail, 16:9 — a serious archaeological-mystery editorial poster, not a fantasy or adventure-game illustration. One complete composition with integrated headline typography and a controlled asymmetric eye path: headline first, the human discovery moment second, the mechanism third.

HEADLINE ZONE (left 36-40% of frame): render the exact two-line headline:

IT SHOULDN'T
EXIST

Both lines left-aligned, stacked with clear breathing room between them — the lines must not crowd each other. "IT SHOULDN'T" is monumental condensed serif lettering. "EXIST" is larger but stops well clear of the bottom frame edge. The letterforms are carved weathered stone with a muted stone-and-bronze base surface — NOT solid bright gold. Cracked, distressed texture. Subtle three-dimensional bevel. A restrained warm-gold highlight catches only the upper-left edge of each letter, as a thin accent light, not a fill color. Dark inner shadow. Restrained black drop shadow. Perfectly spelled, highly legible at 320x180, physically integrated into the scene's lighting rather than pasted on top.

DOORWAY + FIGURE ZONE (center-left, roughly 22-25% of frame, offset left of the vertical midline — not centered): a glowing ancient stone doorway, positioned off-axis, casts localized warm amber light down worn stone steps. A single human silhouette, larger and clearer than a distant background figure, is caught mid-step at the threshold, one hand reaching toward the light as if just discovering it — an active discovery gesture, not a passive stance. Backlit, faceless, creating scale and narrative pull. The light stays contained to this doorway; it does not spread across the frame.

MECHANISM ZONE (right foreground, roughly 35-40% of frame, partially cropped by the right frame edge): a corroded bronze Antikythera-like mechanism, mostly in shadow, with only a few large gear forms and one dial partially caught by rim light — the rest of the object recedes into near-black. This is a glimpse that implies scale and complexity, not a fully-lit, fully-detailed display piece — the mechanism should read as something half-glimpsed and mysterious, not as an object being presented for inspection. Dark verdigris-bronze patina (never turquoise). Warm gold rim lighting on only the nearest edges — the mechanism must NOT become the brightest object in the frame and must NOT dominate through visual density; the headline and the doorway light remain the brighter, busier reads.

Strong near-black separation between all three zones — deep shadow gaps keep the headline, the doorway/figure, and the mechanism visually distinct rather than fused into one block. Asymmetric composition, not a balanced rule-of-thirds grid. One coherent camera perspective, one physically continuous underground chamber, one warm directional light source anchored at the doorway. Restrained, investigative mood throughout — mystery and discovery, not spectacle or grandeur.

No extra text, no random lettering, no inscriptions, no symbols, no watermarks.
```

**Directive appended automatically at generation time** (from `hypogeum.brand.visual_identity.build_visual_identity_directive("thumbnail")`, brand spec version `v1.1`):

```
 — HYPOGEUM BRAND VISUAL IDENTITY (thumbnail, full enforcement): Palette: near-black #080C0E and charcoal #1A1D20 dominate (60-70% of frame); dark stone #2B2E33 and muted bronze #5C4A2F as midground/structural material (20-30%); aged gold #C49A42 as a restrained accent only (5-10%, never a dominant field color); warm ivory #E7D6B3 and off-white #F5F3EE for highlights and text only, never a background or large fill. Forbidden: blue or cyan lighting of any kind; teal lighting; turquoise verdigris (patina rendered as bright cyan-green rather than muted dark bronze-green); orange/yellow scene wash covering the full frame; full-frame sepia tint; bright polished gold (flat, high-value, chrome-like gold with no aging/texture); fluorescent or neon colors of any hue; heavy saturated color cast dominating the overall frame. Finish: premium archaeological documentary, near-black dominant, restrained aged gold, weathered stone and bronze, realistic photographic texture, controlled directional light, deep but readable shadows. Avoid: generic fantasy-art finish, museum-display finish, glossy modern product-render look.
```

No provider call has been made against this prompt. Generation is deferred pending explicit approval, per instruction.
