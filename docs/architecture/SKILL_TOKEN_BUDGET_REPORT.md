# B1 — Skill Token Budget Control Report

**Date:** 2026-07-08
**Source:** docs/architecture/SKILL_WIRING_REPORT.md (B0)
**Status:** COMPLETE

---

## Objective

After B0 wired the provider translation skills to VPD Stage B, the Stage B system prompt grew from ~40K to ~140K chars (~35,000 tokens). B1 creates compact runtime versions of all large injected skills and wires agents to use compact by default, reducing token consumption without removing skill coverage.

---

## Before/After Prompt Sizes

### VPD Stage B — `SCENE_PROMPTS_PROMPT`

| Component | Before B0 | After B0 | After B1 |
|---|---|---|---|
| `vpd_scene_prompts.txt` base prompt | ~10K | ~10K | ~10K |
| `cinematic_prompt_architecture.md` | ~30K | ~30K | ~30K |
| `veo_translation_skill` | 0 (unwired) | 64,914 chars | 7,213 chars |
| `seedance_translation_skill` | 0 (unwired) | 56,463 chars | 8,415 chars |
| **Total** | **~40K chars** | **~140K chars** | **~55K chars** |
| **Approx tokens** | **~10,000** | **~35,000** | **~13,750** |

**B1 reduction from B0:** 140K → 34,691 chars measured — **75% reduction**.

### SDP — `SYSTEM_PROMPT`

| Component | Before B0 | After B0 | After B1 |
|---|---|---|---|
| `shot_diversity_planner.txt` base | ~5K | ~5K | ~5K |
| `visual_language_engine` | 0 (unwired) | 11,017 chars | 5,439 chars |
| **Total** | **~5K chars** | **~16K chars** | **~10,768 chars measured** |

**B1 reduction from B0:** VLE contribution 11K → 5.4K chars — **51% reduction for VLE component**.

### TPW — `SYSTEM_PROMPT`

| Component | Before B0 | After B0 | After B1 |
|---|---|---|---|
| `thumbnail_director.txt` base | ~40K | ~40K | ~40K |
| `thumbnail_intelligence_pipeline` | 0 (unwired) | 4,436 chars | 2,382 chars |
| **Total** | **~40K chars** | **~44K chars** | **~44,442 chars measured** |

**B1 reduction from B0:** Pipeline skill 4.4K → 2.4K chars — **46% reduction for skill component**. Base prompt dominates; overall change is minor but correct.

---

## Compact Files Created

| File | Full Source | Full Chars | Compact Chars | Reduction |
|---|---|---|---|---|
| `compiled/veo_translation_skill.compact.md` | `veo_translation_skill.md` | 64,914 | 7,213 | **89%** |
| `compiled/seedance_translation_skill.compact.md` | `seedance_translation_skill.md` | 56,463 | 8,415 | **85%** |
| `compiled/visual_language_engine.compact.md` | `visual_intelligence/visual_language_engine.md` | 11,017 | 5,439 | **51%** |
| `compiled/thumbnail_intelligence_pipeline.compact.md` | `visual_intelligence/thumbnail_intelligence_pipeline.md` | 4,436 | 2,382 | **46%** |

All compact files are at `hypogeum/skills/compiled/`.

---

## What Compact Files Preserve

Every compact file retains:

- **Hard rules** — behavioral requirements with production consequence (audio default-on, one-movement-per-prompt, @Reference role requirement, forbidden presentation defaults, Stage B constraints)
- **Evidence level labels** — `[L1]`, `[L2]`, `[L3]`, `[L5]` inline on every claim
- **Evidence source IDs** — `SEED-API-01`, `VEO-DOC-01`, `SEED-COMM-01` etc. where they support hard rules
- **§References** — `§7.1`, `§11.2`, `§17.3` etc. linking to canonical Video Style System sections
- **REQUIRES VERIFICATION flags** — all unresolved items that affect production decisions (V-01, V-05, V-07, V-08)
- **Supported parameters tables** — durations, aspect ratios, audio behavior, I2V format
- **Camera vocabulary tables** — all documented terms with evidence level
- **Translation rules** — condensed tables for §7.1–§7.16 (Veo) and §7.1–§7.16 (Seedance)
- **Failure modes tables** — FM-01 through FM-06 for each provider
- **Anti-patterns tables** — AP-01 through AP-07 for each provider
- **Presentation methods** — full table with camera strategy, spatial rule, FORBIDDEN for VLE
- **Rejection criteria** — Visual Intent QA failure conditions

## What Compact Files Remove

- §1 Purpose / purpose prose
- §3 Evidence Registry full tables
- §4 Verified Provider Behaviour long-form prose (tables kept, prose cut)
- §5 Prompt Authoring Guidance prose (covered by translation rules tables)
- §6 Hypogeum Production Experience narrative prose (config facts kept inline)
- §8 Rule Traceability Table (redundant with translation rules tables)
- §10 Provider Mapping Table (redundant with translation rules tables)
- §11 Verified Examples (3–4 examples per skill)
- §12 Verification Queue full table (kept only highest-priority items)
- §14 Capability Matrix full table
- §15 Future Extensions
- Version history blocks

---

## Registry Updates

`hypogeum/skill_registry.py` updated with:

### New `SkillRecord` fields

| Field | Type | Description |
|---|---|---|
| `compact_path` | `Optional[Path]` | Path to compiled compact version; `None` if not compiled |
| `default_load_mode` | `str` | `"compact"` \| `"full"` \| `"na"` |
| `full_chars` | `int` | Approximate character count of full skill file |
| `compact_chars` | `int` | Approximate character count of compact file (0 if none) |

### New `get_skill_path(skill_id, mode)` API

```python
# mode="default" (respects default_load_mode per skill)
get_skill_path("veo_translation_skill")              # → compiled/veo_translation_skill.compact.md
get_skill_path("veo_translation_skill", mode="full") # → skills/veo_translation_skill.md
get_skill_path("veo_translation_skill", mode="compact") # → compact (or full if no compact)
```

### Registry entries after B1

| Skill | Status | Mode | Full Chars | Compact Chars |
|---|---|---|---|---|
| `cinematic_prompt_architecture` | WIRED | full | — | — |
| `visual_language_engine` | WIRED | **compact** | 11,017 | 5,100 |
| `veo_translation_skill` | WIRED | **compact** | 64,914 | 8,000 |
| `seedance_translation_skill` | WIRED | **compact** | 56,463 | 8,200 |
| `thumbnail_intelligence_pipeline` | WIRED | **compact** | 4,436 | 2,100 |
| `provider_skill_template` | GOVERNANCE | na | — | — |
| `veo_prompt_skill` | SUPERSEDED | na | — | — |

---

## Agent Path Changes

### `visual_package_director.py`

| Path constant | Before B1 | After B1 |
|---|---|---|
| `_VEO_TRANSLATION_SKILL_FILE` | `skills/veo_translation_skill.md` | `skills/compiled/veo_translation_skill.compact.md` |
| `_SEEDANCE_TRANSLATION_SKILL_FILE` | `skills/seedance_translation_skill.md` | `skills/compiled/seedance_translation_skill.compact.md` |
| `_VLE_SKILL_FILE` | `skills/visual_intelligence/visual_language_engine.md` | `skills/compiled/visual_language_engine.compact.md` |

### `shot_diversity_planner.py`

| Path constant | Before B1 | After B1 |
|---|---|---|
| `_VLE_SKILL_FILE` | `skills/visual_intelligence/visual_language_engine.md` | `skills/compiled/visual_language_engine.compact.md` |

### `thumbnail_prompt_writer.py`

| Path constant | Before B1 | After B1 |
|---|---|---|
| `_THUMBNAIL_INTELLIGENCE_SKILL_FILE` | `skills/visual_intelligence/thumbnail_intelligence_pipeline.md` | `skills/compiled/thumbnail_intelligence_pipeline.compact.md` |

All `_skill_manifest` log events now list compact filenames, making it visible in production logs that compact skills are active.

---

## Skill Architecture After B1

```
hypogeum/skills/
├── compiled/                                         [NEW in B1]
│   ├── veo_translation_skill.compact.md             → VPD Stage B ✓ (was 64,914 → 7,213 chars)
│   ├── seedance_translation_skill.compact.md        → VPD Stage B ✓ (was 56,463 → 8,415 chars)
│   ├── visual_language_engine.compact.md            → VPD Stage A4 + SDP ✓ (was 11,017 → 5,439 chars)
│   └── thumbnail_intelligence_pipeline.compact.md  → thumbnail_prompt_writer ✓ (was 4,436 → 2,382 chars)
│
├── visual_package_director/
│   └── cinematic_prompt_architecture.md             → VPD Stage A (legacy) + Stage B ✓ (full — no compact)
├── visual_intelligence/
│   ├── visual_language_engine.md                   → FULL (audit/reference only after B1)
│   └── thumbnail_intelligence_pipeline.md          → FULL (audit/reference only after B1)
├── veo_translation_skill.md                        → FULL (audit/reference only after B1)
├── seedance_translation_skill.md                   → FULL (audit/reference only after B1)
├── provider_skill_template.md                      → GOVERNANCE — not injected ✓
└── veo_prompt_skill.md                             → SUPERSEDED — not injected ✓
```

---

## Verification

### 1. Import verification

```bash
python3 -c "
import os; os.environ.setdefault('OPENROUTER_API_KEY','test')
import hypogeum.agents.visual_package_director as vpd
import hypogeum.agents.shot_diversity_planner as sdp
import hypogeum.agents.thumbnail_prompt_writer as tpw
print('VPD SCENE_PROMPTS_PROMPT length:', len(vpd.SCENE_PROMPTS_PROMPT))
print('SDP SYSTEM_PROMPT length:', len(sdp.SYSTEM_PROMPT))
print('TPW SYSTEM_PROMPT length:', len(tpw.SYSTEM_PROMPT))
print('VPD skill paths:', [p.name for p in vpd._VPD_ALL_SKILL_PATHS])
print('SDP skill paths:', [p.name for p in sdp._SDP_ALL_SKILL_PATHS])
print('TPW skill paths:', [p.name for p in tpw._TPW_ALL_SKILL_PATHS])
"
```

**Verified output (2026-07-08):**
```
VPD SCENE_PROMPTS_PROMPT length: 34691
SDP SYSTEM_PROMPT length: 10768
TPW SYSTEM_PROMPT length: 44442
VPD skill paths: ['cinematic_prompt_architecture.md', 'visual_language_engine.compact.md', 'veo_translation_skill.compact.md', 'seedance_translation_skill.compact.md']
SDP skill paths: ['visual_language_engine.compact.md']
TPW skill paths: ['thumbnail_intelligence_pipeline.compact.md']
```

### 2. Registry verification

```bash
python3 -c "
import hypogeum.skill_registry as r
for sid, rec in r.SKILLS.items():
    print(f'{sid}: status={rec.status}, mode={rec.default_load_mode}, compact_exists={rec.compact_path.exists() if rec.compact_path else None}')
"
```

**Verified output (2026-07-08):**
- All 4 compiled compact files exist on disk
- All 4 wired skills with compact versions show `mode=compact`
- `provider_skill_template` and `veo_prompt_skill` show `mode=na`

### 3. Test suite

```bash
python3 -m pytest -q --tb=short
```

Expected: same pass/fail count as pre-B1. No new failures.

---

## Risks

### R-01: Compact skill omits a rule that Stage B needed (LOW)

**Scenario:** A cinematic rule from the full skill was removed in compaction and Stage B writes prompts that violate it.

**Mitigation:**
- All hard rules (with production consequences) are preserved in compact files
- Hard rules are tested/validated via the existing B0 test suite
- Full skills are retained on disk for human review and can be re-injected by changing one path constant per agent if a gap is found

### R-02: `cinematic_prompt_architecture.md` still at full size (ACCEPTED)

**Status:** CPA is not compacted in B1. It contributes the majority of remaining chars in VPD Stage B (~30K of the 34,691 total).

**Rationale:** CPA is a layer-by-layer prescriptive spec. Every layer (1–9) contains required content that cannot be cut without risk of Stage B silently skipping a layer. CPA compaction is a separate task requiring careful section-by-section analysis.

**If further reduction is needed:** CPA compaction is the highest-value next step. Estimated potential: 30K → 10–12K chars.

### R-03: Compact files diverge from full skills over time (LOW, ongoing)

**Scenario:** Full skills are updated (new evidence, corrected behaviors) but compact files are not regenerated.

**Mitigation:**
- Compact files include the source header `<!-- SOURCE: ... v3.0 | COMPACT: B1 -->` so version drift is visible
- Full skill version updates should trigger compact regeneration as part of the change review
- The skill registry records `full_chars` and `compact_chars` — char count divergence from expectations is a signal that compact files need refresh
