# Composite Pipeline Quality Audit

Read-only audit. No file modified, no code changed, no paid call made. All findings are from direct visual inspection of the already-generated files (`ep003_tsc_A_composite_verification01*`) and direct reading of `hypogeum/agents/thumbnail_compositor.py`.

---

## 1. Visual assessment of each layer

- **base_scene** — a complete, coherent underground chamber: warm amber torchlight from a doorway (upper-left), stone steps, shelving, pottery, carved wall diagrams. Single light source, consistent perspective, eye-level-ish wide interior view. Technically a strong standalone image.
- **hero_subject** — the Antikythera mechanism shot in near-macro close-up from a **low, slightly-upward angle**, resting on a dark rocky ledge, against a **solid black rectangular background**. Lighting is a distinct two-tone scheme: warm gold key from the upper-right plus a **cool teal/green rim** on the left side of the mechanism — a highlight temperature that does not appear anywhere in base_scene.
- **fx** — atmospheric gold dust and volumetric light rays angled from the upper-left, on a pure black field, clearly built for screen-blend use.
- **320×180 preview** — the mismatches identified below are substantially less visible at this size; the composite reads as a plausible single thumbnail at a glance.

## 2. Exact compositing method (from `_composite_layers()`, `hypogeum/agents/thumbnail_compositor.py:388-486`)

1. `base_scene` is the foundation canvas, full opacity, no blend.
2. `fx` is resized to full frame and composited with `ImageChops.screen()`.
3. `hero_subject` is resized to a **fixed rectangle** — right 60% of width, full height (`x_frac=0.40, w_frac=0.60, h_frac=1.00`) — placed on a black canvas, **feathered only on its left edge** (6% of frame width, linear fade to black), then the whole rectangle is composited with `ImageChops.screen()`.
4. Typography (PIL-rendered) is placed the same way in the left 40%, screen-blended, feathered only on its right edge.
5. A global color-grade pass and a radial vignette (`ImageChops.multiply()` against a radial-gradient mask, `_apply_vignette()`) are applied last, over the whole frame — unrelated to hero-subject separation.

## 3. Answers to the numbered questions

1. **How is hero_subject separated from its background?** It isn't, in the sense of shape-aware separation. The layer is resized to a fixed rectangle and screen-blended; separation depends entirely on the *source image* having a genuinely black background, so that screen-blend math (`black contributes nothing`) makes that background disappear. The only edge treatment is a 6%-width linear feather on the left side only — top, bottom, and right edges of the rectangle get no feathering at all.
2. **Does a true alpha mask exist for hero_subject?** No.
3. **Is background removal performed?** No — no `rembg`, no cutout library, no post-hoc segmentation of the generated hero image.
4. **Is segmentation performed?** No.
5. **Is the hero layer blended as a full rectangular image?** Yes, confirmed directly from the code — `layer_r = layer.convert("RGB").resize((lw, lh), ...)`, pasted onto a black canvas at a fixed rectangle, then screen-blended.
6. **PIL blend mode:** `ImageChops.screen()` for both `hero_subject` and `fx` (identical mechanism for both).
7. **How is opacity selected?** There is no explicit opacity/alpha parameter anywhere in this path. "Opacity" is entirely an emergent property of screen-blend math plus the one linear edge feather; there is no `ImageEnhance`-based alpha control applied to either layer as a whole.
8. **Do the layers share camera angle / perspective / light direction / focal length / subject scale / color temperature?** No enforcement exists anywhere — not in the prompts, not in the compositing code. Visually confirmed mismatches: base_scene is a wide, eye-level, single-warm-light interior; hero_subject is a low-angle macro shot with an added cool-toned rim light never present in base_scene; the two depict visually distinct rock/stone surfaces (the chamber floor vs. the ledge the mechanism sits on).
9. **Do the prompts enforce compatibility between layers?** No. `base_scene`, `hero_subject`, and `fx` prompts (quoted in the previous turn) contain no shared camera, lighting-direction, or perspective language referencing each other at all — each was written and will be generated as an independent scene.
10. **Does the final result contain:**
    - **Rectangular blending artifacts:** present but substantially masked by the screen-blend-against-black technique and the left-edge feather; most visible on close inspection of the hero region's top/right/bottom edges, not obvious at a glance.
    - **Double backgrounds:** yes — the mechanism's own dark ledge surface is visually distinct from and inconsistent with the chamber's stone floor.
    - **Mismatched lighting:** yes — the cool teal/green rim light on the mechanism has no source or counterpart anywhere in base_scene's single warm-torch lighting scheme.
    - **Scale inconsistency:** plausible/ambiguous — no in-frame element establishes whether the mechanism is meant to be hand-sized or room-filling; the two layers don't reconcile this.
    - **Perspective mismatch:** yes — wide eye-level interior vs. low-angle macro subject shot.
    - **Excessive FX:** not excessive in volume, but positioned over the doorway/headline zone rather than interacting with the mechanism, so it reads as belonging to base_scene rather than unifying the composite.
    - **Artificial cutout appearance:** yes, on close full-size inspection — driven primarily by the lighting/perspective mismatch, not by a visible hard edge (the screen-blend technique does hide the hardest edge artifacts effectively).

11. **Is the final layered composite visually better than one unified GPT Image 2 image?** Mixed. The layered approach's real, working advantage is deterministic control the model can't reliably guarantee on its own: the near-black left-zone reservation and wordmark-safe top zone (both genuinely present and clean in this output). But it introduces a *different* defect class — cross-layer incoherence (lighting direction, perspective, "double surface") — that a single unified generation would not have, because one generation call has one internally-consistent lighting/camera decision by construction. The single-image path's own outputs (reviewed earlier this session) demonstrated internally coherent single-scene lighting, at the cost of weaker deliberate text-zone reservation. Neither path alone currently produces a fully correct result.

## 4. Full-size vs. 320px quality

- **Full size (1280×720):** the cross-layer mismatches (lighting temperature, perspective, double-surface) are visible on direct inspection. The composite is technically successful (all four layers present, correctly positioned) but reads as an assembled image rather than one continuous photograph.
- **320×180 (mobile preview):** the same mismatches are substantially less perceptible — thumbnail scale compresses the lighting/perspective seams below the threshold of easy detection, and because the screen-blend technique keeps true black genuinely black, there is no visible rectangular fringe or box around the hero subject at this size. This is a real, measurable improvement at review scale, but it is a **scale-dependent mitigation of an underlying coherence problem, not a fix of it** — the same source images at any larger display size, or on a larger physical screen, would show the seams again.

## 5. Comparison against the approved reference

Against the "REAL ATLANTIS?" reference's requirements (headline-dominant composition, staged foreground/midground/background, one dominant subject, high mobile clarity): this composite **does** now achieve one dominant subject, a reserved and legible headline zone with real typography applied, and correct wordmark placement — a clear improvement over the earlier single-object hero-shot samples reviewed before this pipeline change. It does **not** yet achieve the reference's staged depth quality (its foreground carved-face element and lit-doorway midground read as one coherent physical space; this composite's chamber and mechanism read as two different physical spaces merged together) or its bold, textured, monumental headline treatment (this headline is comparatively small and thin against the reference's cracked-metal gold lettering, though that is a typography-styling question, not a compositing one).

**Match to approved reference: partial.** Composition-level requirements (subject dominance, headline zone, mobile clarity) are substantially better met than the pre-pipeline-change samples. Depth-coherence and headline-boldness gaps remain.

---

## Recommendation

**D — Hybrid approach: unified base image + minimal deterministic FX.**

Reasoning: Option **B** (fix masks/compatibility) would only address the edge-artifact symptom, not the root cause — a perfect alpha mask around the mechanism would not fix the mismatched lighting direction or the two-different-rock-surfaces problem, because `base_scene` and `hero_subject` are independently imagined scenes with no shared camera/light contract; masking can't manufacture a shared contract that was never specified. Option **C** (fully unified single image, typography only) would recover internal lighting/perspective coherence — as the single-image-path samples already demonstrated — but would give up the layered system's one genuinely working advantage: reliable, deterministic left-zone/top-zone reservation for typography and wordmark, which a single text-only instruction to the model has not reliably achieved elsewhere in this project (documented zone-reservation validator failures in `ROOT_CAUSE_ANALYSIS.md`). A hybrid — generate subject-in-environment as **one** unified image (letting the model own internal lighting/perspective coherence, as it already does successfully for single-scene generation), then layer only genuinely additive, tolerant-of-imperfection elements on top (the `fx` screen-blend, which is forgiving of exact alignment since it's just light/particles, plus the already-fully-deterministic PIL typography and wordmark, which have no coherence problem at all since they don't depict a physical scene) — keeps the layered system's real value while removing the specific merge that is causing the defects documented here. This is a recommendation for a future task; nothing has been implemented.

---

## Answers

1. Final composite inspected: yes
2. True alpha/mask used for hero: no
3. Hero is full-frame image: yes
4. FX blend mode: `ImageChops.screen()`
5. Major visual defects: mismatched lighting temperature (warm-only vs. warm+cool rim), perspective mismatch (wide eye-level vs. low-angle macro), double/inconsistent ground surface, no cross-layer scale reconciliation
6. Quality at 320px: substantially improved / defects largely masked by scale, but not resolved at source
7. Matches reference: partial — subject dominance/headline zone/mobile clarity improved; staged depth coherence and headline boldness still gap
8. Recommended architecture: D — hybrid (unified subject+environment image, deterministic FX/typography/wordmark layered on top)
9. Files modified: none
10. Paid calls made: 0
11. Output path: `docs/validation/thumbnail_image_generation_validation/COMPOSITE_PIPELINE_QUALITY_AUDIT.md`
