# Discovery Director — Implementation Plan

**Created:** 2026-06-23
**Predecessor:** M5.1 — Autonomous discovery loop (signal scoring + queue refill) complete.
**Scope:** LLM-driven topic ideation with structured candidate output, score-based verification, and instant v1 rollback via `DISCOVERY_ENGINE`.

---

## Architecture Corrections (Applied Before Code)

Five constraints govern this implementation. They override any prior informal description:

1. **`DISCOVERY_ENGINE=v1|v2` dispatch** — `run_discovery_cycle()` is the stable public entrypoint and must not be replaced. It dispatches to legacy heuristic (`v1`) or Discovery Director (`v2`) based on env. Instant rollback = `DISCOVERY_ENGINE=v1`.

2. **Score-based verification threshold** — Verification eligibility is data-driven. No fixed count (e.g. "top 5"). A candidate is eligible if `initial_composite >= DISCOVERY_VERIFY_THRESHOLD` OR `initial_composite >= (best_score - DISCOVERY_VERIFY_DELTA)`.

3. **Deterministic `canonical_subject` from `topic_text` only** — The LLM does NOT generate `canonical_subject`. Python computes it deterministically from `topic_text` alone — angle is excluded. Multiple angles on the same underlying subject must resolve to one shared canonical identity. All cache keys, source-verification keys, opportunity deduplication, novelty scoring, discovery memory, and future topic-graph references must use this Python-generated value.

4. **No scores in LLM output** — The Topic Ideator returns exactly six string fields per candidate. No `initial_composite`, no ratings, no numeric output of any kind. Scoring is owned exclusively by `initial_scorer.py` using deterministic Python logic applied after parsing.

5. **`initial_scorer.py` is the sole source of `initial_composite`** — After parsing the LLM batch, every candidate passes through `initial_scorer.compute_score(candidate, signal_ctx)`. The result is attached as `candidate["initial_composite"]`. Any LLM response that includes a numeric score field must have that field stripped before the candidate is processed further.

---

## 1. LLM Output Schema (v2 candidates — Topic Ideator)

The LLM prompt instructs the model to return a JSON array. Each element has exactly these fields — no more, no fewer:

```json
{
  "topic_text":            "string  — the episode subject",
  "angle":                 "string  — unique interpretive lens on the topic",
  "mystery_hook":          "string  — the open question that drives viewer curiosity",
  "differentiation_claim": "string  — why this is distinct from existing coverage",
  "visual_signal":         "string  — primary visual element that defines the thumbnail/opening shot",
  "evidence_signal":       "string  — the anchor fact, artefact, or source that grounds the claim"
}
```

**Hard rules for the Topic Ideator:**
- No numeric fields of any kind — no scores, ratings, rankings, or probabilities.
- The prompt must not ask the model to evaluate, rank, or score candidates.
- Any response element containing a numeric-valued key is rejected at parse time (logged, discarded).
- `canonical_subject` and `initial_composite` are never in the LLM response. Both are computed by Python after parsing.

---

## 2. Deterministic `canonical_subject`

```python
import re

def _make_canonical_subject(topic_text: str) -> str:
    """
    Deterministic canonical subject key derived from topic_text only.

    Angle is intentionally excluded: multiple angles on the same underlying
    subject (e.g. Poseidon angle, Trade angle, Earthquake angle on
    "Lost City of Helike") must resolve to one shared canonical identity.

    Used for: source-verification caching, opportunity deduplication,
    novelty scoring, discovery memory, topic-graph node identity.

    Stable contract: same topic_text always produces the same output,
    regardless of angle or any other candidate field.
    """
    slug = topic_text.strip().lower()
    slug = re.sub(r"[^a-z0-9\s]", "", slug)
    slug = re.sub(r"\s+", "_", slug)
    return slug[:200]
```

**Rules:**
- Signature is `_make_canonical_subject(topic_text)` — one argument only. Passing `angle` is a contract violation.
- All cache lookups, source-verification keys, and topic-graph nodes use this output.
- `canonical_subject` is never passed to the LLM prompt and never parsed from LLM output.
- If `topic_text` changes (e.g. via operator edit), `canonical_subject` must be recomputed. Angle changes do not affect `canonical_subject`.
- Two candidates with identical `topic_text` but different `angle` values have the same `canonical_subject`. This is intentional — they are competing angles on the same opportunity.

---

## 3. Score-based Verification Eligibility

`initial_composite` used here is always the output of `initial_scorer.compute_score()` — never a value from the LLM.

```python
# Config-driven — never hardcoded counts.
DISCOVERY_VERIFY_THRESHOLD = float(os.getenv("DISCOVERY_VERIFY_THRESHOLD", "0.65"))
DISCOVERY_VERIFY_DELTA     = float(os.getenv("DISCOVERY_VERIFY_DELTA",     "0.10"))

def _is_verify_eligible(candidate_score: float, best_score: float) -> bool:
    """
    A candidate is eligible for source verification if either condition holds:
      1. Its initial_composite meets or exceeds the absolute threshold.
      2. Its initial_composite is within DELTA of the best candidate in this cycle.
    Both thresholds use scores produced by initial_scorer.py, never LLM output.
    """
    return (
        candidate_score >= DISCOVERY_VERIFY_THRESHOLD
        or candidate_score >= (best_score - DISCOVERY_VERIFY_DELTA)
    )
```

**Rules:**
- Verification is triggered for all qualifying candidates, not a fixed top-N.
- The `best_score` is the maximum `initial_composite` in the cycle, after the scorer has run on all candidates.
- Verification cost scales with `DISCOVERY_VERIFY_THRESHOLD`; operators lower it to verify more broadly, raise it to verify only the strongest.

---

## 4. Dispatch in `run_discovery_cycle()`

```python
def run_discovery_cycle(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Stable public entrypoint. Dispatches to v1 or v2 based on DISCOVERY_ENGINE.
    v1 is the current heuristic implementation. v2 is the Discovery Director.
    """
    import os
    engine = os.getenv("DISCOVERY_ENGINE", "v1").strip().lower()
    if engine == "v2":
        return _run_discovery_cycle_v2(channel_id, db_path)
    return _run_discovery_cycle_v1(channel_id, db_path)
```

- `_run_discovery_cycle_v1()` = current body of `run_discovery_cycle()`, moved verbatim. No changes.
- `_run_discovery_cycle_v2()` = Discovery Director (see §5).
- Both functions return the same dict shape: `{ok, cycle_id, channel_id, candidate_count, selected_candidate_id, candidates}`.

---

## 5. Discovery Director v2 — `_run_discovery_cycle_v2()`

### Phase 1 — Context gathering
Same as v1: `_get_existing_topics()`, `_build_signal_context()`. No change.

### Phase 2 — LLM ideation (Topic Ideator)
Prompt the LLM with:
- Channel identity / niche description (from channel profile)
- Signal context summary (top-performer keywords, penalty keywords, KPI baselines)
- Recently consumed topics (for exclusion)
- Instructions to return 8–12 structured candidates as JSON array
- Explicit instruction: no scores, no rankings, no numeric fields

Parse the response. Reject any candidate missing required string fields or containing any numeric-valued key. On parse failure or zero valid candidates: log warning, fall back to `_run_discovery_cycle_v1()`, set `engine_used="v1_fallback"`.

Valid required fields per candidate: `topic_text`, `angle`, `mystery_hook`, `differentiation_claim`, `visual_signal`, `evidence_signal`.

### Phase 3 — canonical_subject injection
For each parsed candidate:
```python
candidate["canonical_subject"] = _make_canonical_subject(candidate["topic_text"])
```
`angle` is not passed. Two candidates with the same `topic_text` and different angles will share one `canonical_subject` — this is correct and intentional.

### Phase 4 — Initial scoring via `initial_scorer.py`
For each candidate, compute the score using signal context and existing topic history:
```python
candidate["initial_composite"] = initial_scorer.compute_score(candidate, signal_ctx, existing_topics)
```
`initial_scorer.compute_score()` is deterministic Python — no LLM calls, no randomness. It uses:
- Keyword overlap with `signal_ctx` (top-performer boost, penalty keywords)
- Novelty penalty if `canonical_subject` matches a recently consumed topic
- Base scores derived from structural signals in `mystery_hook` and `differentiation_claim`

After all candidates are scored:
```python
best_score = max(c["initial_composite"] for c in candidates)
```

### Phase 5 — Score-based verification
For each candidate where `_is_verify_eligible(c["initial_composite"], best_score)`:
- Call source-verification tool (Perplexity or equivalent)
- Attach `verification_result: {verified: bool, source_url: str | None, confidence: str}`
- If verification fails: apply penalty to `initial_composite` (equivalent to `_SIGNAL_PENALTY_LOW_RETENTION`)

Unverified candidates retain `verification_result: None`. Score is not adjusted.

### Phase 6 — Final selection and DB write
- Re-sort by `initial_composite` after verification adjustments.
- Mark `selected=1` on the highest scorer.
- Insert into `topic_candidates` with all new fields.
- Return standard result dict plus `engine_used="v2"`.

---

## 6. DB Schema Changes

New columns on `topic_candidates` (added via migration, nullable to preserve v1 rows):

```sql
ALTER TABLE topic_candidates ADD COLUMN canonical_subject TEXT;
ALTER TABLE topic_candidates ADD COLUMN angle TEXT;
ALTER TABLE topic_candidates ADD COLUMN mystery_hook TEXT;
ALTER TABLE topic_candidates ADD COLUMN differentiation_claim TEXT;
ALTER TABLE topic_candidates ADD COLUMN visual_signal TEXT;
ALTER TABLE topic_candidates ADD COLUMN evidence_signal TEXT;
ALTER TABLE topic_candidates ADD COLUMN verification_result TEXT;   -- JSON blob
ALTER TABLE topic_candidates ADD COLUMN engine_used TEXT DEFAULT 'v1';
```

Constraint: v2 rows MUST have `canonical_subject NOT NULL`. Enforced in `_run_discovery_cycle_v2()`, not at DB level (to avoid breaking v1 rows).

---

## 7. New Config Vars

Added to `hypogeum/config.py` under the `# --- Autonomous Discovery ---` block:

| Env var | Default | Type | Purpose |
|---|---|---|---|
| `DISCOVERY_ENGINE` | `v1` | str | Dispatch: `v1` = heuristic, `v2` = Director |
| `DISCOVERY_VERIFY_THRESHOLD` | `0.65` | float | Absolute score floor for verification eligibility |
| `DISCOVERY_VERIFY_DELTA` | `0.10` | float | Relative delta from best score for eligibility |

---

## 8. Module Changes

| File | Change |
|---|---|
| `hypogeum/topic_discovery.py` | Add `_make_canonical_subject(topic_text)` (one arg), add `_is_verify_eligible()`, rename current body to `_run_discovery_cycle_v1()`, add `_run_discovery_cycle_v2()`, update `run_discovery_cycle()` to dispatch |
| `hypogeum/initial_scorer.py` | **New file.** Exports `compute_score(candidate, signal_ctx, existing_topics) -> float`. Deterministic Python only — no LLM calls. Replaces the `_score_seed()` role for v2 candidates. |
| `hypogeum/config.py` | Add `DISCOVERY_ENGINE`, `DISCOVERY_VERIFY_THRESHOLD`, `DISCOVERY_VERIFY_DELTA` |
| `hypogeum/db.py` | Add migration for new `topic_candidates` columns |

`initial_scorer.py` is a standalone module (not nested under a package) so it can be unit-tested and evolved independently of the Director. If `_run_discovery_cycle_v2()` exceeds ~200 lines, extract to `hypogeum/discovery_director.py`.

---

## 9. Test Requirements

| Test | Assertion |
|---|---|
| `canonical_subject` is deterministic | Same `topic_text` → same output across calls |
| Same subject, different angles → same `canonical_subject` | `_make_canonical_subject("Lost City of Helike")` is identical for Poseidon, Trade, and Earthquake angle candidates |
| `canonical_subject` is never LLM-generated | v2 result dict `canonical_subject` matches `_make_canonical_subject(topic_text)` — angle is not in it |
| LLM response with numeric field → candidate rejected | A candidate containing any numeric key (e.g. `"initial_composite": 0.8`) is discarded at parse time; logged |
| `initial_composite` comes from scorer, not LLM | LLM stub returns only 6 string fields; result candidates have `initial_composite` set by `initial_scorer.compute_score()` |
| Scorer is deterministic | Same `(candidate, signal_ctx, existing_topics)` → same `initial_composite` on repeated calls |
| Scorer produces no LLM calls | `initial_scorer.compute_score()` with mocked LLM verifier confirms zero LLM invocations |
| `DISCOVERY_ENGINE=v1` uses heuristic path | Mocked LLM is never called |
| `DISCOVERY_ENGINE=v2` calls LLM | LLM stub is invoked; result has `engine_used="v2"` |
| Verification is score-based | Candidate at threshold − 0.01 is NOT verified; candidate at threshold IS verified |
| Verification delta path | Candidate below threshold but within delta of best IS verified |
| LLM parse failure → v1 fallback | Garbled LLM response triggers fallback; `engine_used="v1_fallback"` |
| Zero valid candidates after parse → v1 fallback | All candidates contain numeric fields and are rejected; fallback fires |
| v1 rows unaffected | `canonical_subject` is NULL for v1-sourced rows in DB |
| Both dispatch paths return same dict shape | `{ok, cycle_id, channel_id, candidate_count, selected_candidate_id, candidates}` |

---

## 10. Implementation Sequence

1. DB migration — new `topic_candidates` columns (backward compatible, all nullable)
2. `_make_canonical_subject(topic_text)` + `_is_verify_eligible()` + new config vars
3. Rename existing body to `_run_discovery_cycle_v1()`
4. Dispatch stub in `run_discovery_cycle()`
5. `hypogeum/initial_scorer.py` — `compute_score()` + unit tests (no LLM dependency)
6. Topic Ideator: LLM prompt (6 string fields only, explicit no-score instruction) + parser with numeric-field rejection (Phase 2)
7. canonical_subject injection in Phase 3 — `_make_canonical_subject(topic_text)` only
8. Wire `initial_scorer.compute_score()` in Phase 4
9. Source verification integration (Phase 5)
10. Full `_run_discovery_cycle_v2()` assembly (Phase 6)
11. Integration tests (dispatch, fallback, end-to-end)

Steps 1–4 are non-breaking and can be committed independently. Step 5 (`initial_scorer.py`) can be developed and tested before any LLM integration. Steps 6–11 require `DISCOVERY_ENGINE=v2` to activate.
