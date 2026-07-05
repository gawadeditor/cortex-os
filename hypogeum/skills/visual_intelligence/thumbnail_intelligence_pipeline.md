# Thumbnail Intelligence Pipeline — Stage Reference

**Version**: 1.0
**Owner**: thumbnail_director
**Pipeline position**: After Visual Package Director Phase 1; before Phase 2 (MUAPI generation)

This document describes the official Thumbnail Intelligence Pipeline. It is the authoritative reference for the Thumbnail Director's execution sequence.

---

## Pipeline Overview

```
CTR Strategy (thumbnail_strategy.txt)
    ↓
Curiosity Strategy (concept_brief: viewer_promise, curiosity_gap)
    ↓
Composition Planner (thumbnail_composition_planner.py)
    ↓
Typography Planner (thumbnail_typography_planner.py)
    ↓
Identity Validator (TIG — thumbnail_identity_guardian.py)
    ↓
Thumbnail QA (thumbnail_qa.py)
    ↓
Candidate Ranking (rank field in thumbnail_generation_plan)
    ↓
MUAPI Generation (image generation via muapi.py)
    ↓
Final Selection (thumbnail_package: main_thumbnail + alt_thumbnails)
```

---

## Stage Descriptions

### 1. CTR Strategy
**File**: `hypogeum/prompts/thumbnail_strategy.txt`
**Purpose**: Claude determines the Click-Through Rate strategy for the thumbnail based on the episode topic, viewer psychology, and platform context.
**Key output fields**:
- `ctr_strategy`: string describing the primary hook (curiosity gap, pattern interrupt, etc.)
- `primary_emotion_target`: the emotion the thumbnail should evoke
- `title_relationship`: how the thumbnail and title work together

### 2. Curiosity Strategy (Concept Brief)
**Purpose**: Translate the CTR strategy into a concrete visual concept.
**Key output fields in `concept_brief`**:
- `viewer_promise`: what the viewer implicitly promises themselves when they click ("I will learn X")
- `curiosity_gap`: the specific unknown that creates compulsion to click
- `visual_hook`: the concrete visual element that delivers the curiosity gap

### 3. Composition Planner
**File**: `hypogeum/agents/thumbnail_composition_planner.py`
**Purpose**: Define the spatial arrangement of elements in the thumbnail frame.
**Key decisions**:
- Foreground/background hierarchy
- Text placement zones (safe areas for platform overlays)
- Subject positioning (rule of thirds, golden ratio)
- Negative space allocation

### 4. Typography Planner
**File**: `hypogeum/agents/thumbnail_typography_planner.py`
**Purpose**: Define text overlay strategy.
**Key decisions**:
- Text content (3–5 words maximum)
- Font weight and size relationship to frame
- Colour contrast for legibility at thumbnail scale (168×94px minimum legible)
- Drop shadow and outline specifications

### 5. Identity Validator (TIG)
**File**: `hypogeum/agents/thumbnail_identity_guardian.py`
**Purpose**: Brand Guardian pass for thumbnails. Verifies the planned thumbnail composition aligns with channel visual identity before generation credits are spent.
**Checks**:
- Colour palette compliance (warm earth tones dominant)
- Typography style compliance (no sans-serif for historical content)
- Logo placement (lower-right safe zone)
- No fluorescent or clinical colour temperature

### 6. Thumbnail QA
**File**: `hypogeum/agents/thumbnail_qa.py`
**Purpose**: Post-generation quality check.
**Checks**:
- Dimensions (1280×720 minimum)
- Asset class (main_thumbnail vs alt_thumbnail)
- File path present on disk
- Text overlay legibility score
- Brand compliance score

### 7. Candidate Ranking
**Output field**: `rank` in `thumbnail_generation_plan.candidates[]`
**Purpose**: Claude ranks all generated candidates from most to least likely to achieve high CTR. The rank=1 candidate becomes `main_thumbnail` in the `thumbnail_package`.
**Ranking criteria**:
- Curiosity gap strength
- Visual clarity at small size
- Colour differentiation from typical feed competition
- Text legibility

---

## Integration Contracts

- Thumbnail Director reads `thumbnail_handoff_brief` from Visual Package — never the full Visual Package
- TIG runs before MUAPI generation credits are spent (same principle as BGA Pass 1 for video)
- Thumbnail QA runs after generation, before `thumbnail_package` is sealed
- Final `thumbnail_package` is the sole input to the Publishing Agent's thumbnail upload step

---

## Output Schema

The `thumbnail_package` schema (validated in `hypogeum/schemas.py`) requires:
- `main_thumbnail.asset_id` — must be non-empty
- `main_thumbnail.width` and `.height` — 1280×720 minimum
- `thumbnail_qa_report.overall_status` — pass/warn/fail
- At least 1 `alt_thumbnails` entry for A/B testing
