# ep_f3619022 Thumbnail Repair — 2026-08-23 (Phase 2)

Status: deterministic recovery succeeded for all four deliverables at the
pixel-QA level; **no MUAPI call was made, $0.00 spent** of the $0.54
authorized cap. No live Zernio mutation was made. No worker restart,
queue change, or autonomous-publishing enablement occurred.

This phase also hardens `ZernioProvider.repair_existing_post()` — see
§4.

## 0. Canonical aspect-ratio contract (confirmed, unchanged)

- `main_episode` → horizontal **16:9** (`1280×720`) — the long-form
  YouTube thumbnail.
- `short_001` / `short_002` / `short_003` → vertical **9:16**
  (`720×1280`) — the Short/Reel/TikTok cover, per
  `hypogeum.muapi._THUMBNAIL_TARGETS` and
  `hypogeum.agents.thumbnail_qa`'s own aspect-aware feed-scale targets
  (`_FEED_SCALE_LANDSCAPE`/`_FEED_SCALE_PORTRAIT`). No Short thumbnail
  was converted to `1280×720`.

## 1. Deterministic recovery — no regeneration needed

All four already-existing thumbnail candidates
(`thumbnails/candidate_1.jpg` for main, `thumbnails/short_00{1,2,3}.jpg`
for the Shorts — all previously produced by Thumbnail Director, all
genuinely on-topic and on-brand) were failing
`c_thumbnail_feed_scale_legibility` for the same underlying reason: an
extremely dark exposure (`mean_luma` 9–16 out of 255) that collapses to
near-black once downscaled to feed-thumbnail size, even though the full
resolution image contains real, legible, brand-correct content (candlelit
archives, a relief map of tunnel entrances across Europe, a squeeze
passage, a manuscript scroll).

A single deterministic levels correction (gamma=1.5 in linear space,
0.015 black-floor lift, applied identically to all four — no per-image
tuning) was applied via local PIL processing — **the same pixel content,
no new imagery, no paid call**:

| Deliverable | Source | Corrected file | Dimensions | SHA-256 |
|---|---|---|---|---|
| main_episode | `thumbnails/candidate_1.jpg` | `main_episode.recomposed_v1.jpg` | 1280×720 | `d223fcf58d73a013a853dfef4b62c8ae237b5879ad664fa741d4f0c4142ad12` |
| short_001 | `thumbnails/short_001.jpg` | `short_001.recomposed_v1.jpg` | 720×1280 | `83a5a7c4d50462a2866f46744ff44843ce47252e8f263e0f0cc09ce26f4b1ff` |
| short_002 | `thumbnails/short_002.jpg` | `short_002.recomposed_v1.jpg` | 720×1280 | `7bdf1d9ffc6811e36dd54144d3da70d10cfd4ae44179584fa0c53e099b5090a` |
| short_003 | `thumbnails/short_003.jpg` | `short_003.recomposed_v1.jpg` | 720×1280 | `23b051a9cd9e71ae23ebcd076590bee7f7089bc15ad013f049a93f4eb1a88d5` |

Full files: `docs/operations/repairs/thumbnails/`. Never overwrites
`candidate_1.jpg` or any `short_00N.jpg` in place — every corrected file
is a new, immutably-versioned (`.recomposed_v1`) artifact.

## 2. QA results — real `run_thumbnail_qa()`, aspect-correct

Run with `aspect_ratio="16:9"` for main and `aspect_ratio="9:16"` for
each Short (never rejected for not being horizontal):

| Deliverable | Dimensions check | Placeholder detection | Feed-scale legibility | Overall |
|---|---|---|---|---|
| main_episode | pass | pass | **pass** (dark=0.51, legible=0.10) | warn* |
| short_001 | pass | pass | **pass** (dark=0.01, legible=0.12) | warn* |
| short_002 | pass | pass | **pass** (dark=0.51, legible=0.10) | warn* |
| short_003 | pass | pass | **pass** (dark=0.08, legible=0.16) | warn* |

\* `overall_status="warn"` only from `c_thumbnail_text_overlay` (skipped
— no `generation_plan` available for a post-hoc recomposition, not a
failure). `failures: []` for all four — this is what
`hypogeum.agents.production_output_gate._check_thumbnail()` itself
treats as passing (`if qa_report.get("failures"): fail else: pass`).

## 3. Identity guardian results — real Claude vision call, all four

| Deliverable | Verdict | Blocking violations |
|---|---|---|
| main_episode | **WARNING** (0 blocking) | 2 warnings only (font/silhouette prominence notes) |
| short_001 | **BLOCKED** (3 blocking) | TIG-05/06/07 — no text overlay, no wordmark, "not 1280×720 landscape" |
| short_002 | **BLOCKED** (3 blocking) | same three |
| short_003 | **BLOCKED** (3 blocking) | same three |

**Finding:** `hypogeum.agents.thumbnail_identity_guardian.
run_thumbnail_identity_guardian()` has no `deliverable_class`/aspect
parameter — it applies the SAME rule set (mandatory 16:9 landscape,
mandatory "THE HYPOGEUM CHRONICLES" wordmark, mandatory 3-line
typographic hierarchy) to every image regardless of whether it is the
main episode's thumbnail or a Short's vertical cover. All three
Shorts' BLOCKED verdicts stem entirely from this — not from any real
brand/content violation (no unrelated imagery, no prohibited human
figure — none of the three Short images contains a human figure at all,
and main_episode's own silhouetted figure is back/profile-only, which
is the explicitly PERMITTED pattern per
`hypogeum/brand/video_style_system.md`'s scale-anchor rule, never a
frontal face).

This is reported honestly as a real BLOCKED verdict from the current
tool, not silently overridden. `thumbnail_status` for all three Shorts
is therefore recorded as `"fail"` in the manifest, and their repair
mutation stays gated closed — pending either (a) a deliverable-class-aware
identity guardian, out of this phase's scope, or (b) an explicit human
override for the vertical-Short contract.

## 4. `ZernioProvider.repair_existing_post()` hardening

Before this phase, the platform-entry merge REBUILT every targeted
platform's entry via `_build_platform_entry()` — including an already-
succeeded platform like `youtube`, silently discarding its real
`platformSpecificData`, `contentHash`, provider `_id`, `status`, and
`publishAttempts` fields. Fixed: every existing `platforms[]` entry is
now carried into the PUT body byte-for-byte; only a platform genuinely
absent from the existing post gets a freshly built minimal entry
appended. See `hypogeum/publishing/zernio.py`'s updated docstring and
merge logic.

Four new tests in `TestRepairPreservesExistingPlatformEntriesByteForByte`
(`tests/test_zernio_short_repair.py`) prove: an existing entry with
undocumented fields survives unchanged; only genuinely-missing platforms
get a freshly built (minimal) entry; the repaired post ID stays
identical and zero POST calls occur; and an untouched platform's rich
entry survives even when that specific repair call doesn't target it.
48/48 tests in this file pass.

## 5. Contact sheet

`docs/operations/repairs/thumbnails/contact_sheet.jpg` — full-resolution
image, feed-scale preview (aspect-correct downscale, same target sizes
`run_thumbnail_qa()` uses), dimensions, QA verdict, identity verdict,
SHA-256 prefix, and provenance, grouped by deliverable. The full,
machine-readable manifest (including complete identity violation text)
is `docs/operations/repairs/EP_F3619022_THUMBNAIL_MANIFEST_2026-08-23.json`.

## 6. Remaining blockers before updating the four existing Zernio posts

1. **main_episode**: thumbnail now passes both gates
   (`mutation_would_proceed: true` for the thumbnail dimension alone) —
   but main's title/content were already correct, so no PUT is actually
   needed for main at all; the corrected thumbnail is ready to attach
   whenever the repaired-media staging question (§7 below) is resolved
   for the episode as a whole.
2. **short_001/002/003**: still blocked — the identity guardian's
   aspect-unaware rule set blocks all three (§3). Resolve via a
   deliverable-class-aware identity check or an explicit human override.
3. Media staging: the repaired local videos
   (`.hypogeum-assets/episodes/ep_f3619022/repair/short_00N.repaired.mp4`,
   produced in the prior phase) and these corrected thumbnails have
   **not been uploaded** — no live presign/upload call was made in this
   phase either. Real public URLs are required before any PUT body is
   more than a proposal.
4. Once (2) and (3) are resolved, re-run
   `validate_repair_preconditions()` with the real staged URLs and
   passing thumbnail status — only then does `mutation_would_proceed`
   become `true` for the Shorts.
5. Explicit authorization for a live `repair_existing_post()` PUT
   remains a separate, later, not-yet-authorized phase.
