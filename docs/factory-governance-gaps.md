# Factory Governance Gaps

**Version:** v1.0
**Status:** Operational reference. Updated when enforcement changes.
**Last updated:** 2026-06-12

This document records what the Hypogeum factory enforces in code, what is enforced
only by prompt instruction, what is only documented, and what is not enforced at all.
It is the authoritative gap register for planning future enforcement work.

---

## Enforcement Layers (implemented)

```
Layer 1 — VPD system prompt (inline)   pre-generation text rules to Claude
Layer 2 — TD system prompt (inline)    pre-generation text rules to GPT
Layer 3 — BGA Pass 1                   pre-generation LLM text scan of scene descriptions
Layer 4 — BGA Pass 2                   pre-generation LLM text scan of thumbnail candidates
Layer 5 — TIG                          post-generation vision gate on rendered thumbnail
Layer 6 — Thumbnail QA                 deterministic structural checks (dimensions, paths)
Layer 7 — Reference Image QA           deterministic structural checks (dimensions, paths)
Layer 8 — QA Agent                     deterministic package integrity gate
Layer 9 — Compliance Agent             deterministic AI disclosure / platform metadata gate
```

---

## What Is Enforced in Code

| Rule | Layer(s) | Mechanism | Hard Block? |
|---|---|---|---|
| No human face as primary thumbnail subject | BGA Pass 2 (E-TH01), TIG (TIG-01) | LLM text scan + vision | Yes |
| No bright/high-key background in thumbnail | BGA Pass 2 (E-TH07), TIG (TIG-02) | LLM text scan + vision | Yes |
| No neon/cyberpunk/sci-fi language | BGA Pass 1 (E-C03–C08), BGA Pass 2, TIG (TIG-03) | LLM text scan + vision | Yes |
| No talking head / lip sync / direct-to-camera | BGA Pass 1 (E-VG05/VG06) | LLM text scan | Yes |
| Wordmark present in thumbnail | BGA Pass 2 (E-L01–L09), TIG (TIG-05/07) | LLM text scan + vision | Yes |
| Thumbnail dimensions 1280×720 / 16:9 | Thumbnail QA | Deterministic | Warning only |
| Background brightness 0–40% | TIG (TIG-02/W01) | Vision | Yes (BLOCKING) |
| No warm-cool palette violations (text) | BGA Pass 1/2 | LLM text scan | Yes |
| AI disclosure present | Compliance Agent | Deterministic text check | Yes |
| Platform metadata (title non-empty) | Compliance Agent | Deterministic | Yes |
| Narration audio present | Audio QA | Deterministic | Yes |
| MUAPI budget ceiling | MUAPIBudgetGuard | Hard limit | Yes |

---

## What Is Enforced Only by Prompt

These rules are communicated to the generative model via system prompt text. There
is no downstream code check to verify the model followed them.

| Rule | Prompt Location | Risk |
|---|---|---|
| Warm earth tone palette dominant | VPD prompt (inline), TD prompt (inline) | Model may ignore; BGA/TIG catches obvious violations only |
| No cool-blue / green / pink palette | VPD + TD prompts | BGA catches if named explicitly; TIG checks brightness not hue sweep |
| Lighting: warm/atmospheric/underground only | VPD prompt | No post-generation lighting gate beyond TIG brightness |
| No flat/fluorescent/overexposed lighting | VPD prompt | Same as above |
| Historical accuracy constraints | VPD prompt | No automated check; entirely model-trust |
| No face-forward humans (incl. mummies) | VPD prompt | BGA catches talking-head/direct-camera patterns; TIG catches thumbnail faces |
| Text safe zones (Shorts scenes) | VPD field `text_safe_zone` | Assembly uses this field; no gate validates that field was used correctly |
| Cinzel/Raleway typography (when unnamed) | BGA catches only if font explicitly named in prompt | Silently passes if prompt names no font |

---

## What Is Only Documented (not enforced)

These rules exist in `docs/channel-identity.md` or `brand_bible_hypogeum.md` but
no pipeline agent enforces them.

| Rule | Document | Enforcement Status |
|---|---|---|
| Exact hex palette values (void_black #080C0E, antique_gold #C49A42, etc.) | channel-identity.md §1 | Not enforced. BGA spec marks all hex values NOT_VERIFIED. |
| Cinzel SemiBold / Raleway Regular font weights (E-T10–T12) | brand_spec_executable.md | Acknowledged unenforceable. No font-weight inspection in generated images. |
| Font hierarchy (three-line wordmark structure) | channel-identity.md §3 | Checked by BGA Pass 2 only if wordmark structure is described in prompt text. |
| Video overlay opacity 0.70–0.92 | channel-identity.md §1 | Not enforced. |
| Subtitle line length ≤ 42 chars, 1.5–7 s duration | channel-identity.md §6 | Not enforced in Caption Agent. |
| Emotional register prohibitions (sensationalism, horror, comedy) | channel-identity.md §4 | Not enforced in any gate. Script Engine prompt includes tone guidance only. |
| Title max 60 characters for SEO | channel-identity.md §7 | Not enforced. Compliance Agent checks title non-empty only. |
| Brand Bible activation checklist | brand_bible_hypogeum.md | Checklist incomplete; Brand Bible marked "Inactive — not yet enforced". |

---

## What Is Not Enforced (acknowledged gaps)

| Gap | Category | Planned? |
|---|---|---|
| **Exact hex color validation in generated output** | Color palette | Not planned. Pixel-level hex matching in JPEG output is unreliable due to compression. Enforcement requires a visual similarity approach, not exact match. |
| **Actual font rendering validation (Cinzel/Raleway)** | Typography | Not planned for current milestones. Requires OCR or font-classification model. Acknowledged in brand_spec_executable.md as unenforceable at generation time. |
| **BGA Pass 3 — post-render pixel audit** | Brand compliance | Deferred. No implementation timeline. Pass 3 would close the gap between text-scan enforcement and actual rendered output. |
| **Full palette sweep in TIG** | Color palette | TIG checks background brightness (0–40%) but does not sweep the full image for forbidden hue ranges (cool blues, greens, pinks). |
| **VPD / TD prompts not loaded from channel-identity.md** | Single source of truth | By design (frozen prompts). Updates to channel-identity.md are NOT automatically reflected in VPD or TD system prompts. Manual sync required. |
| **Emotional register enforcement** | Tone | No automated gate. Relies on Script Engine and Narrative Architect prompt instructions only. |
| **VPD re-planning on BGA rejection** | Recovery | v1A halts on BGARejectedError. Auto-revise loop is a future capability (not scheduled). |

---

## Required Future Work

### BGA Pass 3 (post-render pixel audit)

**Status:** Deferred. No milestone assigned.

BGA Pass 3 would inspect generated scene video frames and reference images for
visual identity compliance after MUAPI generation. This would close the largest
remaining gap: rules that are only enforced via prompt text today.

**Minimum scope for Pass 3:**
- Detect forbidden hue ranges (cool blue, green, pink, neon) in video frames
- Detect bright/high-key lighting in non-thumbnail outputs
- Flag talking-head framing in generated video
- Palette distance check against the 7-swatch palette

### Font / Typography Visual Validation

**Status:** Not planned.

Verifying that Cinzel SemiBold and Raleway Regular were actually rendered in the
generated output requires either:
- OCR + font classification on a text region crop
- Structural comparison of glyph shapes (expensive, brittle)

Until this exists, font enforcement is text-only (BGA blocks when font is explicitly
named wrong in prompt).

### Hex Color Validation

**Status:** Not planned.

Exact hex matching in JPEG/WebP output is not reliable. A practical implementation
would use L*a*b* color distance with a tolerance threshold for each of the 7 swatches.
This could be added as a lightweight deterministic check in Thumbnail QA or as a
new TIG rule.

### Single-Source Identity Injection

**Status:** Not planned.

Currently, visual identity rules in VPD and TD prompts are hardcoded inline. Any
change to `docs/channel-identity.md` requires a manual update to both system
prompts. A future architecture could inject the canonical rules from the identity
doc at prompt-construction time (e.g., loading a specific section of channel-identity.md
into the system prompt before the LLM call).

---

## Governance Note on Prompt-Based Enforcement

Prompt-based enforcement is probabilistic. A model may follow instructions
imperfectly, especially for subjective properties (palette warmth, mood register).
The BGA and TIG layers exist to catch violations that slip through prompt instruction.
Any rule that is prompt-only should be considered best-effort, not guaranteed.

The only guaranteed enforcement is code-level: deterministic QA checks, schema
validation, and TIG vision gates with fail-closed behavior.
