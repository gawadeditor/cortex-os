# Thumbnail Intelligence Pipeline — Compact Runtime Reference
<!-- SOURCE: hypogeum/skills/visual_intelligence/thumbnail_intelligence_pipeline.md v1.0 | COMPACT: B1 -->
<!-- PURPOSE: Runtime-only condensed version. Full skill retained for audit/reference. -->

## Pipeline Sequence

```
CTR Strategy → Curiosity Strategy → Composition Planner → Typography Planner
    → Identity Validator (TIG) → Thumbnail QA → Candidate Ranking
    → MUAPI Generation → Final Selection (thumbnail_package)
```

## Stage Summary

| Stage | File | Key Output |
|---|---|---|
| 1. CTR Strategy | `prompts/thumbnail_strategy.txt` | `ctr_strategy`, `primary_emotion_target`, `title_relationship` |
| 2. Curiosity Strategy | concept_brief | `viewer_promise`, `curiosity_gap`, `visual_hook` |
| 3. Composition Planner | `thumbnail_composition_planner.py` | Foreground/background hierarchy, text zones, subject positioning, negative space |
| 4. Typography Planner | `thumbnail_typography_planner.py` | Text (3–5 words max), font weight, colour contrast, drop shadow specs |
| 5. Identity Validator (TIG) | `thumbnail_identity_guardian.py` | Brand compliance: warm earth palette, no sans-serif, logo in lower-right, no fluorescent colour |
| 6. Thumbnail QA | `thumbnail_qa.py` | Dimensions ≥1280×720, asset class, file path exists, legibility score, brand compliance |
| 7. Candidate Ranking | `rank` field in generation plan | rank=1 → `main_thumbnail`; criteria: curiosity gap, clarity at small size, colour differentiation, text legibility |
| 8. MUAPI Generation | `muapi.py` | Generated candidate images |
| 9. Final Selection | `thumbnail_package` | `main_thumbnail` + `alt_thumbnails` (≥1 required) |

## Integration Contracts

- Thumbnail Director reads `thumbnail_handoff_brief` from Visual Package — never the full Visual Package
- TIG runs **before** MUAPI generation (same principle as BGA Pass 1 for video)
- Thumbnail QA runs **after** generation, before `thumbnail_package` is sealed
- `thumbnail_package` is the sole input to the Publishing Agent's thumbnail upload step

## Output Schema Requirements

`thumbnail_package` (validated in `hypogeum/schemas.py`) requires:
- `main_thumbnail.asset_id` — must be non-empty
- `main_thumbnail.width` and `.height` — 1280×720 minimum
- `thumbnail_qa_report.overall_status` — pass / warn / fail
- At least 1 `alt_thumbnails` entry for A/B testing
