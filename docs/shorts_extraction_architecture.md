# Shorts Extraction Architecture — Design Proposal

**Status:** Proposal — awaiting approval before implementation  
**Milestone:** M6 scope  
**Replaces:** Dedicated Short scene generation (current model)

---

## Problem

Current architecture generates three categories of video content per episode:

1. Main episode scenes (16:9, Tier 1–3, 5–8s each) — ~12–51 scenes
2. Dedicated Short scenes (9:16, Tier 3, 5–8s each) — ~6–15 scenes (3 Shorts × 2–5 scenes)
3. Thumbnail images — kept as-is

Short scenes are generated at the same provider cost as main scenes. For a 5-scene validation run with `shorts_count=0`, we already suppressed this. But a full production run currently pays for both categories.

**Concrete cost example (20-scene episode, 3 Shorts × 3 scenes each):**
- Main generation: 20 scenes × $0.15 = $3.00
- Short generation: 9 scenes × $0.15 = $1.35
- Total: $4.35 — 31% of cost is Shorts generation

The Shorts generation also multiplies every failure mode: more scenes → more retries → more fallback spend.

---

## Proposed Model

```
Generate Main Episode Only (16:9)
        ↓
Assemble Master Episode
        ↓
Extract Shorts from Main Clips
        ↓
Publish Main + Shorts
```

Main episode scenes carry extraction metadata. After main assembly, a Shorts Extractor step selects, crops, and assembles Short videos from the already-generated main clips. No additional video generation.

---

## Component Changes

### 1. Visual Package Director (VPD)

**Remove:** All 9:16 Short scene generation. The `scenes[]` array no longer contains any `source_type: "short"` entries.

**Add to each main scene's `visual_spec`:**
```json
{
  "short_eligible": true,
  "short_crop_focus": "center"
}
```

- `short_eligible: bool` — whether this scene is a candidate for Short inclusion. Set `true` for visually strong scenes (artefact close-ups, dramatic moments, REVELATION scenes). Set `false` for transitional/background scenes.
- `short_crop_focus: "left" | "center" | "right"` — where the subject sits in the 16:9 frame, so the 9:16 crop captures it. Default `"center"`. Use `"left"` or `"right"` when the primary subject is off-centre.

**Add a `shorts_extraction_plan[]` to the Visual Package:**
```json
{
  "shorts_extraction_plan": [
    {
      "short_id": "sh_01",
      "title": "The Hidden Chamber Discovery",
      "scene_ids": ["s03", "s07", "s11"],
      "extraction_rationale": "Strongest visual sequence for vertical format"
    },
    {
      "short_id": "sh_02",
      "title": "Ancient Technology Revealed",
      "scene_ids": ["s04", "s08", "s14"],
      "extraction_rationale": "Close-up artefact sequence — minimal crop loss"
    },
    {
      "short_id": "sh_03",
      "title": "The Unanswered Question",
      "scene_ids": ["s01", "s15", "s20"],
      "extraction_rationale": "Opening + revelation + mystery hook for retention"
    }
  ]
}
```

VPD selects 3–5 main scenes per Short based on: `short_eligible=true`, visual weight, narrative arc, and minimal crop loss risk.

**Remove from VPD:** `thumbnail_handoff_brief.short_candidates[]` — Short thumbnails are now extracted from the main clip renders, not planned separately.

**Remove from GENERATION CONSTRAINTS:** `shorts_count` becomes permanently irrelevant — Shorts are always zero-cost extracted products. The constraint key can be removed from the schema in a future cleanup.

---

### 2. Script Engine

**Keep:** `shorts_scripts[]` — these remain as editorial guides. Each entry still contains:
- `short_id`
- `title`
- `hook_line` (first line of the Short)
- `narration_beats[]` — condensed voice-over for the Short (~45–90s total, 3–4 beats)

The Script Engine no longer needs to produce full independent short scripts. The narration beats are shorter — just a hook + 2–3 beats that map to the selected main scenes.

This is a **prompt change to the Script Engine**, not a schema change. The SE still produces `shorts_scripts[]`; the beats are just fewer and tied to the extraction plan.

---

### 3. Audio Agent

**Keep:** Short narration track generation via `skip_shorts: bool`.

The short narration tracks are still valuable — they are purpose-written voice-over for the Short format (tighter, punchier, with a faster hook). This is different from slicing main narration.

`skip_shorts=False` in full production mode. `skip_shorts=True` in Episode Zero / validation mode.

No change needed here.

---

### 4. Assembly Agent

**Phase A (existing):** Render main episode as today.

**Phase B (new): Shorts Extraction step**

After main episode assembly completes, the Shorts Extractor:

1. Reads `shorts_extraction_plan[]` from the Visual Package.
2. For each Short:
   a. Loads the individual rendered scene clips (`sc_<scene_id>.mp4`) for the selected `scene_ids`.
   b. Crops each clip from 16:9 → 9:16 using `crop_focus` from VPD:
      - `center` → `crop=1080:1920:420:0` (ffmpeg: crop width=1080 at x=420 from 1920-wide source)
      - `left`   → `crop=1080:1920:0:0`
      - `right`  → `crop=1080:1920:840:0`
   c. Concatenates the cropped clips.
   d. Mixes in the short narration track (from Audio Agent's `sh_XX` stitched track).
   e. Applies Short-specific text overlay (title card at `text_safe_zone` position).
   f. Writes `short_0_<episode_id>.mp4`.

The crop math: 1920×1080 → crop to 1080-wide × 1920-tall = vertical format. Since source is 1080px tall, the vertical dimension requires scaling. The ffmpeg filter chain is:

```
crop=1080:1080:<x_offset>:0, scale=1080:1920:flags=lanczos, setsar=1
```

This center-crops the 16:9 frame to a 1:1 square, then scales to 9:16. The subject is preserved if `crop_focus` is set correctly.

**Alternative (higher quality):** VPD provides `short_crop_x_pct` (0.0–1.0) as a precise crop anchor rather than left/center/right. This allows finer framing. Initial implementation uses the three-step enum; this can be upgraded later.

---

### 5. Schema Changes

**`visual_package` schema additions:**
- `scenes[].visual_spec.short_eligible: bool` (optional, default false)
- `scenes[].visual_spec.short_crop_focus: "left"|"center"|"right"` (optional, default "center")
- `shorts_extraction_plan: array` (new top-level key)

**`visual_package` schema removals:**
- `scenes[]` entries with `source_type: "short"` — no longer produced
- `episode_metadata.scene_count_shorts` → becomes always 0; can be removed in future
- `thumbnail_handoff_brief.short_candidates[]` → becomes always `[]`

**`final_render_package` schema:**
- `shorts_renders[]` — keeps existing structure; Extractor writes to same fields
- `shorts_source_model: "extracted" | "generated"` — new field to distinguish the two approaches (useful during transition when some episodes use old model, some new)

---

## What This Eliminates

| Cost Driver | Current | Proposed |
|---|---|---|
| Short scene generation | 6–15 API calls | 0 |
| Short scene retries/fallbacks | up to 2× per scene | 0 |
| Short scene planning (VPD) | complex 9:16 scene derivation | simple tagging pass |
| Validation run cost (Shorts) | $0 (already suppressed by skip_shorts) | $0 |
| Full production cost (Shorts) | ~30% of episode generation budget | $0 |

---

## What This Requires

| Requirement | Effort |
|---|---|
| VPD prompt: add `short_eligible`, `short_crop_focus`, `shorts_extraction_plan` | Low — prompt addition |
| VPD schema: add new fields, remove Short scene fields | Medium — schema + validator changes |
| Assembly Agent: add Shorts Extraction step | Medium — ffmpeg crop + concat logic |
| Audio Agent: no change | None |
| Script Engine: shorten short narration beats | Low — prompt change |
| Pipeline: remove `shorts_count` from generation_constraints | Low — config cleanup |
| Tests: update for new extraction model | Medium — existing Short tests need updating |

Total estimated implementation scope: **M6 milestone, ~1–2 days of implementation**.

---

## Migration / Transition

The `skip_shorts` flag remains valid during transition. Validation runs continue to use `skip_shorts=True`. The first full production episode after M6 will use the extraction model.

Episode Zero Lite can run as-is (with `skip_shorts=True`) before M6 is implemented. The extraction architecture does not need to be complete before the next validation run.

---

## Open Questions for Approval

1. **Short narration approach:** Keep dedicated short narration beats (current) or slice main narration at chapter boundaries (simpler)? Dedicated is better quality; sliced is zero-TTS-cost.
2. **Crop quality:** Three-step enum (`left/center/right`) or continuous `crop_x_pct`? Enum is simpler; continuous is more precise.
3. **Short thumbnails:** Extracted from main renders (auto-frame from best Short clip) or keep the Thumbnail Director producing Short thumbnails separately? Auto-frame is cheaper; TD-generated is higher quality.
