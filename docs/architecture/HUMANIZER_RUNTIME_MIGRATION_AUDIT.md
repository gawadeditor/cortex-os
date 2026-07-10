# Humanizer Runtime Migration Audit

**Date:** 2026-07-10
**Status:** READ-ONLY AUDIT — no code, prompt, schema, or test modified by this document. No adapter created. No skill wired or activated.
**Trigger:** Installation of the pinned upstream Humanizer skill (`third_party/skills/humanizer/1b48564898e999219882660237fde01bf4843a0f/`) surfaced that `hypogeum/humanizer.py` — a pre-existing, already-active, already-wired deterministic module — already performs a form of humanization in production. This document audits that existing implementation before any migration decision is made.
**Method:** Graphify-first orientation (5 targeted queries), then direct reads of every file in scope, including the full pinned upstream `SKILL.md` (623 lines, all 33 numbered patterns) for line-by-line comparison against `hypogeum/humanizer.py`'s 6 implemented pattern groups.

---

## 1. Existing Runtime Architecture

### 1.1 `hypogeum/humanizer.py` — full inventory

| Element | Detail |
|---|---|
| Module docstring's own claim | "Deterministic text humanizer (v2.7.0 patterns)... Patterns applied (subset of humanizer v2.7.0)" |
| Public functions | `humanize_field(text: str, field_name: str = "") -> str` (line 133) — applies all 6 pattern groups to one string; `humanize_beats(beats: list) -> list` (line 189) — walks a beat list, applies `humanize_field` to each beat's `"text"` key |
| Internal helpers | `_compile()`, `_compile_removals()` (pattern-compilation helpers, module-import-time) |
| Pattern tables (module-level constants) | `_DASH_PATTERNS` (3 rules), `_INFLATION_PATTERNS` (8 rules), `_VOCABULARY_PATTERNS` (10 rules), `_DISCOURSE_MARKERS` (9 rules), `_FILLER_PATTERNS` (7 rules), `_HEDGING_PATTERNS` (6 rules) — 43 individual regex substitutions across 6 groups, all compiled once at module import time (lines 116–121) |
| No LLM calls, no network calls | Confirmed — pure `re` module usage, no imports beyond `re` and `__future__` |

### 1.2 Stated version/source and provenance verifiability

The docstring cites "humanizer v2.7.0" with no URL, no author, no commit reference, and no license acknowledgment. **Provenance is not stated but is independently verifiable** — see §3 for the full comparison. Verified fact: the group labels `P01`, `P07`, `P14`, `P20`, `P23`, `P24` correspond **exactly** to pattern numbers `#1`, `#7`, `#14`, `#20`, `#23`, `#24` in the pinned upstream `SKILL.md` (v2.8.2), and multiple example phrase pairs are reproduced verbatim (e.g. upstream §23 "In order to achieve this goal" → "To achieve this", "Due to the fact that it was raining" → "Because it was raining", "At this point in time" → "Now"; upstream §24's example sentence "It could potentially possibly be argued that the policy might have some effect on outcomes" is the direct source of `hypogeum/humanizer.py`'s `"could potentially possibly" → "could"` regex). This is not circumstantial — the numbering and phrase overlap across 6 independent pattern groups make coincidental independent derivation implausible. **Provenance is verifiable, and it verifies as a direct, unattributed derivative of blader/humanizer**, not an independently-conceived ruleset.

### 1.3 Every import and call site (confirmed via `grep` across the full repository, not sampled)

| File | Line(s) | Usage |
|---|---|---|
| `hypogeum/agents/script_engine.py` | 42 (import), 129–133 (call) | `for section in script_package.get("sections", []): humanize_beats(section.get("narration_beats", []))` and the same for `shorts_scripts` |
| `hypogeum/publishing/metadata.py` | 18 (import), 143, 167, 239, 261, 267, 291 | Six separate call sites: AI disclosure text, logline (2 functions), episode title, short title (2 branches: dict and list forms) |

**No other file in `hypogeum/` imports `hypogeum.humanizer`.** This is a second, independent, already-active integration point beyond Script Engine — `hypogeum/publishing/metadata.py` calls `humanize_field()` directly at publish-metadata-construction time, entirely separate from the Script Engine beat-humanization path.

### 1.4 Exact placement inside `script_engine.py` — precise execution chain

```
run_script_engine(narrative_package, config, episode_id, output_dir)
  │
  ├─ log "se_script_started"
  │
  ├─ script_package = run_agent(...)          ← hypogeum/agent.py:149
  │     │
  │     ├─ call_claude()                       (LLM call)
  │     ├─ record_cost()
  │     ├─ json.loads(raw_content)              (parse)
  │     ├─ validate("script_package", package)  ← SCHEMA VALIDATION HAPPENS HERE
  │     ├─ _write_package(package, out_path)    ← DISK WRITE HAPPENS HERE, BEFORE RETURN
  │     ├─ log "agent_run_completed"
  │     └─ return package                       (pre-humanization content)
  │
  ├─ for section in script_package["sections"]:
  │     humanize_beats(section["narration_beats"])   ← HUMANIZATION HAPPENS HERE,
  │ for short in script_package["shorts_scripts"]:      AFTER schema validation,
  │     humanize_beats(short["narration_beats"])        AFTER disk write, NEVER re-written to disk
  │
  ├─ log "se_script_completed"
  └─ return script_package                      (humanized, in-memory only)
```

**Answers to the required placement questions:**

- **Before or after validation?** Humanization runs **after** schema validation (`validate("script_package", package)` inside `run_agent()`, step 4 of 6). There is **no re-validation** after humanization mutates the text — the schema is checked once, against pre-humanized content.
- **Before or after persistence?** Humanization runs **after** the on-disk write (`_write_package()`, step 5 of 6, inside `run_agent()`). **The persisted checkpoint file (`script_package_{episode_id}.json`) contains pre-humanized text.** Humanization exists only in the in-memory object returned by `run_script_engine()` to its caller.
- **Before downstream consumption?** Yes, within a single uninterrupted pipeline run — the humanized in-memory `script_package` is what flows to Visual Package Director, Audio Agent, Assembly, etc. in `pipeline.py`, since those calls happen after `run_script_engine()` returns.

### 1.5 Mutation semantics

`humanize_beats()` **mutates the input list's dict entries in place** (each `beat["text"] = humanize_field(...)`) and returns the same list object (`tests/test_humanizer.py::TestHumanizeBeats::test_returns_same_list_object` confirms `result is beats`). `script_engine.py` does not reassign `script_package["sections"]` — it relies entirely on this in-place mutation of the nested beat dicts.

### 1.6 Runs once or multiple times — retries/checkpoint resume

**Runs at most once per Script Engine LLM call, and is silently skipped entirely on checkpoint resume.** Verified directly in `hypogeum/pipeline.py` (the `_run_script_engine` wrapper, lines ~1365–1390):

```python
pkg = _load_package(episode_id, "script_package", output_dir)
if pkg is not None:
    log.info("pipeline_stage_skipped", agent="script_engine", reason="existing_output")
    return pkg
return run_script_engine(narrative_package=..., config=..., episode_id=..., output_dir=...)
```

If `script_package_{episode_id}.json` already exists on disk (the standard checkpoint-resume condition, documented in `.claude/skills/ep0-run.md`'s checkpoint table), the pipeline **returns the loaded file directly and never calls `run_script_engine()` at all** — meaning `humanize_beats()` never executes on that code path. Combined with §1.4's finding that the persisted file is pre-humanization content, this produces a confirmed, concrete gap:

> **On any checkpoint-resume of an episode whose Script Engine stage already completed, the narration beats that flow to every downstream stage (VPD, Audio, Assembly, Publishing) are the raw, un-humanized LLM output — silently, with no error, warning, or distinguishing log field.** The `pipeline_stage_skipped` log event gives no indication that humanization (a post-processing step, not the stage itself) was also skipped as a side effect.

### 1.7 Are cached/checkpointed outputs already humanized?

**No.** Directly answered by §1.4 and §1.6: the checkpoint file written to disk is captured before humanization runs, and the checkpoint-resume path never re-runs humanization. Any episode resumed after a Script Engine checkpoint carries un-humanized narration for the rest of its production life, including whatever eventually gets baked into audio narration and published metadata sourced from that `script_package`.

---

## 2. Current Behavior Inventory

Every rule group, per the actual pattern tables in `hypogeum/humanizer.py`, in application order:

### P14 — Em/en dash replacement (HARD CONSTRAINT, applied first)

| # | Regex | Input pattern | Output | Test coverage |
|---|---|---|---|---|
| 1 | `(\w)—(\w)` | word—word (em dash, no spaces, between word chars) | `word, word` | `TestP14EmDash::test_em_dash_between_words_becomes_comma` |
| 2 | `\s*—\s*` | any other em dash | `, ` | `test_em_dash_with_spaces_removed`, `test_multiple_em_dashes` |
| 3 | `\s*–\s*` | en dash | ` - ` | `test_en_dash_replaced` |

- **Purpose:** matches upstream §14 exactly, including the "hard constraint" framing.
- **Risk of meaning change:** Low — dash-to-comma/hyphen substitution is a punctuation-only transform.
- **Risk of punctuation/quote damage:** Low-medium — rule 1's `(\w)—(\w)` inserts a comma with no verification the resulting sentence remains grammatical; a dash used for a genuine parenthetical aside (not a comma-equivalent pause) could produce an awkward but not fact-altering sentence.
- **Risk of names/dates/numbers being altered:** None — dashes are structural punctuation, not content.
- **Field scope:** Applies to `narration` and `cta` (via `humanize_beats`) and separately to `title`, `logline`, `ai_disclosure` (via `metadata.py`'s direct calls) — all fields uniformly, no field-specific exclusion logic exists.
- **Tested:** Yes, 6 dedicated tests.

### P01 — Significance-inflation phrase removal

8 fixed phrase→phrase substitutions (e.g. `"stands as a testament to"` → `"shows"`, `"pivotal moment"` → `"moment"`, `"groundbreaking discovery"` → `"discovery"`).

- **Purpose:** matches upstream §1 exactly (same phrase list overlap: "testament to", "pivotal moment").
- **Risk of meaning change:** Low for the listed phrases — each replacement preserves the sentence's factual claim while removing only the inflating modifier.
- **Risk of punctuation/quote damage:** None.
- **Risk of names/dates/numbers being altered:** None — patterns match only the fixed inflation phrases, not surrounding content.
- **Field scope:** Uniform across all fields.
- **Tested:** Yes, 5 dedicated tests, including a case-insensitivity check.

### P07 — Overused AI vocabulary replacement

10 fixed word/phrase→word substitutions (`"delve into"`→`"explore"`, `"showcase"`→`"show"`, `"meticulous(?:ly)?"`→`"careful"`, `"seamlessly"`→`""` (deletion), `"intricate"`→`"complex"`, `"elaborate"`→`"detailed"`, `"leverage"`→`"use"`, etc.).

- **Purpose:** matches upstream §7 exactly (word list overlap: delve, showcase, meticulous, intricate — all appear in both).
- **Risk of meaning change:** **Medium for a documentary context specifically.** `"intricate"`→`"complex"` and `"elaborate"`→`"detailed"` are near-synonyms, but "meticulous" → "careful" and "leverage" → "use" can shift register in ways that may not always match documentary narration's intended tone (a deliberate stylistic choice by Script Engine's prompt could be silently overridden).
- **Risk of punctuation/quote damage:** None.
- **Risk of names/dates/numbers being altered:** None.
- **Field scope:** Uniform across all fields.
- **Tested:** Yes, 6 dedicated tests.

### P20 — Chatbot discourse marker removal

9 removal patterns (`"Certainly[!,.]?\s*"`, `"I hope this helps[!,.]?\s*"`, `"It['']s (?:worth noting|important to note) that\s*"`, etc.).

- **Purpose:** matches upstream §20 exactly (word-list overlap: "I hope this helps", "Of course!", "Certainly!").
- **Risk of meaning change:** Low — these are meta-commentary phrases with no factual content of their own; removal is generally safe.
- **Risk of punctuation/quote damage:** Low — sentence-start removal could occasionally leave a lowercase word at the new sentence start if the following text wasn't originally capitalized independently (not verified by any test).
- **Risk of names/dates/numbers being altered:** None.
- **Field scope:** Uniform across all fields.
- **Tested:** Yes, 5 dedicated tests, including a mid-sentence false-positive check.

### P23 — Filler phrase simplification

7 fixed phrase→phrase substitutions (`"in order to"`→`"to"`, `"due to the fact that"`→`"because"`, `"prior to"`→`"before"`, `"subsequent to"`→`"after"`, etc.).

- **Purpose:** matches upstream §23 exactly, with identical before/after example pairs reused verbatim from the upstream `SKILL.md`.
- **Risk of meaning change:** Low — these are close-to-lossless simplifications for the listed phrases.
- **Risk of punctuation/quote damage:** None.
- **Risk of names/dates/numbers being altered:** None.
- **Field scope:** Uniform across all fields.
- **Tested:** Yes, 5 dedicated tests.

### P24 — Excessive hedging reduction

6 patterns, several of which **delete the entire hedging clause** rather than simplify it (`"it could be argued that"` → `""`, `"one might (?:suggest|argue) that"` → `""`, `"it (?:is|seems) (?:clear|evident|obvious) that"` → `""`).

- **Purpose:** matches upstream §24 exactly, same example sentence as the direct source of the "could potentially possibly" → "could" pattern.
- **Risk of meaning change:** **This is the single highest-risk rule group in the current implementation.** Deleting `"it could be argued that"` entirely, rather than softening it, converts a hedged/attributed claim into an unqualified assertion — e.g. `"It could be argued that the policy affected outcomes"` becomes `"the policy affected outcomes"` after this rule fires, a genuine change in epistemic strength, not merely a style fix. For a documentary pipeline whose Research Director explicitly tracks evidence confidence and whose narration is expected to reflect uncertainty where the research package itself is uncertain, this pattern group is a direct, concrete risk to §4's "uncertainty language removal" concern — not theoretical, verifiably present in the shipped code today.
- **Risk of punctuation/quote damage:** None.
- **Risk of names/dates/numbers being altered:** None.
- **Field scope:** Uniform across all fields — meaning this hedging-deletion risk applies identically to narration, CTAs, titles, loglines, and AI disclosure text.
- **Tested:** Yes, 3 dedicated tests, but **no test checks the semantic consequence of clause deletion** — all three tests assert only that the trigger phrase is absent from the output, never that the surrounding claim's epistemic status is preserved or flagged.

### Coverage summary

43 individual regex rules across 6 groups. All 43 are unit-tested at the string-transformation level (`tests/test_humanizer.py`, 61 test methods total across all classes). None are tested for semantic/factual preservation beyond structural dict-shape checks (§5).

---

## 3. Upstream Comparison

The pinned upstream `SKILL.md` (v2.8.2) defines exactly 33 numbered patterns. Classification of all 33 against the existing deterministic implementation:

| # | Upstream pattern | Classification |
|---|---|---|
| 1 | Undue Emphasis on Significance, Legacy, Broader Trends | **Already implemented faithfully** (P01, subset of phrases) |
| 2 | Undue Emphasis on Notability and Media Coverage | **Absent** — requires judging whether a citation list is used for undue authority-signaling, contextual |
| 3 | Superficial Analyses with -ing Endings | **Absent** — requires parsing sentence structure for tacked-on participial phrases, semantic |
| 4 | Promotional and Advertisement-like Language | **Absent** — the word list (vibrant, nestled, breathtaking, etc.) is a candidate for a future regex extension, but none are implemented today |
| 5 | Vague Attributions and Weasel Words | **Requires LLM judgment** — distinguishing a vague attribution from a legitimate paraphrase requires understanding whether a specific source exists |
| 6 | Outline-like "Challenges and Future Prospects" Sections | **Requires LLM judgment** — a structural/section-level pattern, not a sentence-level phrase |
| 7 | Overused "AI Vocabulary" Words | **Partially implemented** (P07 covers 10 of upstream's ~20 listed words: delve, showcase, meticulous, intricate, elaborate, leverage present; actually/additionally/align with/crucial/emphasizing/enduring/enhance/fostering/garner/highlight/interplay/key/landscape/tapestry/testament/underscore/valuable absent) |
| 8 | Avoidance of "is"/"are" (Copula Avoidance) | **Requires LLM judgment** — requires recognizing when a copula was avoidable, contextual |
| 9 | Negative Parallelisms and Tailing Negations | **Requires LLM judgment** — structural sentence-pattern recognition |
| 10 | Rule of Three Overuse | **Requires LLM judgment** — requires counting semantically-parallel items, not a string match |
| 11 | Elegant Variation (Synonym Cycling) | **Requires LLM judgment** — requires tracking whether multiple terms refer to the same referent across a passage |
| 12 | False Ranges | **Requires LLM judgment** — requires judging whether two endpoints form a meaningful scale |
| 13 | Passive Voice and Subjectless Fragments | **Requires LLM judgment** — grammatical voice detection and active-voice rewriting is not regex-expressible without a full parser |
| 14 | Em Dashes (and En Dashes): Cut Them | **Already implemented faithfully** (P14) |
| 15 | Overuse of Boldface | **Unsuitable for deterministic regex replacement in this pipeline** — narration beats are plain prose (no markdown), so this pattern has no applicable surface in Hypogeum's current fields |
| 16 | Inline-Header Vertical Lists | **Unsuitable** — same reasoning as #15, no markdown list surface in narration text |
| 17 | Title Case in Headings | **Absent, but mechanically simple** — a genuine candidate for a cheap regex/string-method addition if Hypogeum ever has heading-like fields; not currently applied anywhere |
| 18 | Emojis | **Absent, but mechanically simple** — a Unicode-range strip is trivially regex-expressible; not currently implemented despite being one of the easiest upstream patterns to port |
| 19 | Curly Quotation Marks | **Absent, but mechanically simple** — a direct character substitution (`"`/`"` → `"`), arguably the single easiest pattern in the whole upstream list to add; not implemented |
| 20 | Collaborative Communication Artifacts | **Already implemented faithfully** (P20, subset of phrases) |
| 21 | Knowledge-Cutoff Disclaimers and Speculative Gap-Filling | **Requires LLM judgment** — distinguishing "stated as unknown" from "invented plausible filler" requires world knowledge, not pattern matching |
| 22 | Sycophantic/Servile Tone | **Requires LLM judgment** — tone assessment |
| 23 | Filler Phrases | **Already implemented faithfully** (P23, matching examples reused verbatim) |
| 24 | Excessive Hedging | **Implemented differently** (P24) — upstream's own example (§24) shows *softening* ("could potentially possibly be argued" → "may"), while hypogeum/humanizer.py's implementation frequently **deletes** the hedging clause outright rather than softening it (see §2's risk note) — a meaningfully different, higher-risk transformation than what upstream itself demonstrates |
| 25 | Generic Positive Conclusions | **Requires LLM judgment** — requires recognizing vague upbeat framing as a category |
| 26 | Hyphenated Word Pair Overuse | **Requires LLM judgment** — requires distinguishing attributive vs. predicate position, a syntactic distinction |
| 27 | Persuasive Authority Tropes | **Absent** — word-list based, a plausible future regex candidate, not currently implemented |
| 28 | Signposting and Announcements | **Absent** — phrase-list based, a plausible future regex candidate, not currently implemented |
| 29 | Fragmented Headers | **Unsuitable** — requires understanding heading/paragraph structural relationship |
| 30 | Diff-Anchored Writing | **Requires LLM judgment** — requires understanding whether prose narrates a change vs. describes a current state |
| 31 | Manufactured Punchlines and Staccato Drama | **Requires LLM judgment** — requires counting a *run* of short fragments and judging rhetorical effect |
| 32 | Aphorism Formulas | **Requires LLM judgment** — requires recognizing a rhetorical formula pattern, high false-positive risk for naive regex |
| 33 | Conversational Rhetorical Openers | **Requires LLM judgment** — requires distinguishing a theatrical pause-and-reveal from ordinary casual phrasing (upstream's own "Detection Guidance" section explicitly warns against flagging "Honestly"/"look" out of context) |

**Coverage tally:** 4 of 33 already implemented faithfully (#1, #14, #20, #23), 1 partially implemented (#7), 1 implemented differently and with materially higher risk than upstream's own demonstrated behavior (#24), 3 absent-but-mechanically-simple (#17, #18, #19 — notably *not yet taken* despite being the easiest to add), 4 absent with no current implementation (#2, #4, #27, #28), 2 unsuitable given Hypogeum's plain-prose field types (#15, #16, #29), and **18 of 33 explicitly require LLM/contextual judgment** and cannot be soundly approximated by deterministic regex at all.

### Classification of `hypogeum/humanizer.py` as a whole

**It is a provably-derived, deliberately narrow deterministic subset of blader/humanizer's pattern list — not a faithful implementation, not a loose imitation, not an independently-derived ruleset, and its provenance is not "impossible to determine."** Evidence for each classification rejected:

- Not **faithful**: covers only 4–5 of 33 patterns fully/partially (12–15%), and one of those (#24) diverges from upstream's own demonstrated behavior in a way that increases risk rather than replicating it.
- Not **loose**: the phrase-level and example-level overlap is exact, not approximate — "loose imitation" would imply paraphrased or reinterpreted rules; these are direct ports of specific pattern IDs with reused example text.
- Not **independently-derived**: ruled out definitively by the pattern-numbering match (P01/P07/P14/P20/P23/P24 = upstream #1/#7/#14/#20/#23/#24) combined with verbatim example-phrase reuse across multiple independent pattern groups — the probability of this occurring by independent convergent design is negligible.
- Provenance **is** determinable: it required cross-referencing against the now-pinned upstream file to prove, which is exactly what this audit did.

The most accurate description: **a mechanically-filtered extraction — someone (a prior Claude session or a human) selected the subset of blader/humanizer's ~2.7.0-era patterns that could be expressed as fixed-phrase regex substitutions (i.e., patterns with a small, enumerable trigger list and an unambiguous replacement), and implemented only those, leaving every pattern requiring semantic judgment unimplemented.** This is a reasonable engineering strategy for a deterministic, LLM-free, zero-cost pass — but it was executed without attribution to the source it was derived from, which is the exact anti-pattern this Skills Standardization Phase's Installation Policy (`SKILLS_AND_AGENTS_AUDIT.md` §6, restated in every subsequent document in this phase) was written to prevent going forward. This predates that policy's articulation in this repository and is reported here as a factual finding, not a new violation.

---

## 4. Current Integration Risks

| Risk | Assessment |
|---|---|
| **Double-humanization risk** | Low within a single run (each field is processed exactly once per `run_script_engine()` call, confirmed by code structure) — but see idempotency note below: the string-level transforms are self-consistent under repeat application (`TestIdempotent` confirms this at the unit level), so even if double-invoked, no compounding corruption would occur. Not a live risk today given single-call-site-per-run structure. |
| **Meaning drift** | Low for P01/P14/P20/P23 (narrow, low-ambiguity phrase substitutions); **Medium for P07** (near-synonym substitution can shift register); **Medium-High for P24** (clause deletion, not softening — see §2 and §3's #24 entry) |
| **Verified-fact corruption** | Low — no implemented pattern touches numeric content, proper nouns, or factual claims directly; all substitutions target modifier phrases or discourse markers, not facts themselves |
| **Names/dates/quotes/numbers corruption** | Low — confirmed by direct regex inspection; none of the 43 rules have a trigger pattern that could match a proper noun, date, or number. No test explicitly proves this (§5), but the regex patterns are inspectable and none are broad enough to accidentally match content-bearing tokens |
| **Evidence-level corruption** | **Medium** — P24's hedging-clause deletions can remove the specific phrase that carried an evidence qualifier (e.g. "it could be argued that X" → "X"), converting a qualified claim into an unqualified one. This is the audit's primary evidence-level finding. |
| **Uncertainty-language removal** | **Medium-High, same root cause as evidence-level corruption above** — this is the same P24 risk restated from the "preserve uncertainty language" angle explicitly named in the task's own required evaluation criteria |
| **Documentary tone flattening** | Medium — P07's vocabulary substitutions (e.g. "meticulous" → "careful") are generic replacements not tuned to a documentary register specifically; Hypogeum's Script Engine prompt may have deliberately chosen a word this pass then overrides |
| **Schema corruption** | Low — all transforms are string-in, string-out on already-string fields; structural dict/list shape is untouched (confirmed by `TestJSONStructureUnchanged`'s 8 tests), and no re-validation occurs after mutation (a process gap, not a demonstrated failure) |
| **Token impact** | None — deterministic, no LLM call, zero additional token cost |
| **Latency impact** | Negligible — pure regex substitution on already-short beat text, sub-millisecond per call |
| **Retry/idempotency behavior** | String-level idempotency is unit-tested and holds for the 6 implemented groups (`TestIdempotent`); **pipeline-level idempotency across a retry is untested** — no test simulates `run_script_engine()` being invoked twice for the same episode outside the checkpoint-skip path |
| **Checkpoint/resume behavior** | **Confirmed, concrete, highest-severity finding of this audit** — see §1.6/§1.7. Checkpoint-resumed episodes silently receive un-humanized narration for the remainder of production, including audio and publishing. |
| **Observability gaps** | Confirmed — no log event exists anywhere for "beats humanized" or "humanization skipped due to checkpoint." The `pipeline_stage_skipped` event correctly reports the Script Engine LLM-call skip but says nothing about the humanization side-effect also being skipped, since from the code's perspective these are two different things happening to overlap silently. |
| **Rollback difficulty** | Low, if the module itself needed to be reverted — it's a single file with no external state; but the *checkpoint-resume gap* has no rollback concept at all, since it's a behavioral gap in already-shipped code, not a pending change |
| **Interaction with ElevenLabs narration** | Direct — humanized (or, on checkpoint-resume, un-humanized) `text` fields are what `voice_generation_operator.py`/`audio_agent.py` synthesize into speech. Neither this audit's file list nor any grep found ElevenLabs-side awareness of whether its input text was humanized. |
| **Interaction with visual directions / downstream scene planning** | None found — `humanize_beats()` only touches `narration_beats[].text`; scene/visual-direction fields in `script_package` are untouched by any humanizer call site, confirmed by the pattern tables' complete absence of any visual-vocabulary trigger phrases |

---

## 5. Existing Test Coverage

`tests/test_humanizer.py` — 11 test classes, 61 test methods total (counted directly from the file):

| Test class | What it actually proves |
|---|---|
| `TestP14EmDash` (6 tests) | Em/en dash substitution produces expected character removal and comma/hyphen insertion for the specific example strings tested |
| `TestP01Inflation` (5 tests) | The 5 sampled inflation phrases (of 8 total) are removed and replaced as specified, including one case-insensitivity check |
| `TestP07Vocabulary` (6 tests) | 6 sampled vocabulary words (of 10 total) substitute correctly |
| `TestP20Discourse` (5 tests) | 4 sampled discourse markers removed correctly, plus one false-positive guard (mid-sentence "We are confident" not stripped) |
| `TestP23Filler` (5 tests) | 5 sampled filler phrases (of 7 total) substitute correctly |
| `TestP24Hedging` (3 tests) | 3 sampled hedging patterns (of 6 total) trigger correctly — **no test checks the semantic consequence of the deletion-vs-softening distinction flagged in §2/§3** |
| `TestIdempotent` (2 tests) | Calling `humanize_field` twice on the same already-humanized text produces identical output; clean text (no matches) round-trips unchanged. **String-level only — no pipeline-level idempotency test exists.** |
| `TestEdgeCases` (5 tests) | Empty string, whitespace-only, no-match text, field-name-independence, and one duplicate empty-string case all behave safely |
| `TestHumanizeBeats` (8 tests) | In-place mutation confirmed, `beat_type=="cta"` routes to the `"cta"` field label, same-list-object identity confirmed, non-dict/missing-text/non-string-text entries safely skipped, unrelated dict keys (`beat_id`, `beat_type`, arbitrary `extra`) untouched, empty list handled |
| `TestJSONStructureUnchanged` (9 tests) | Top-level keys, section structure, beat IDs, beat types, `chapter_markers`, and `script_integrity` are all untouched by calling `humanize_beats` on nested `narration_beats` — proves structural isolation, not semantic preservation |
| `TestMetadataIntegration` (9 tests) | `hypogeum/publishing/metadata.py`'s 4 functions correctly apply humanization to episode titles, short titles (both dict and list forms), YouTube/Facebook descriptions, and AI disclosure text; confirms the AI disclosure sentence itself survives humanization intact |

### Missing tests, evaluated against every category the task requires

| Required category | Present? | Note |
|---|---|---|
| Idempotency | **Partial** — string-level only; no test invokes `run_script_engine()` twice or simulates a retry |
| Fact preservation | **Missing** — no test asserts that a specific factual claim (e.g. a date, a numeric claim, a causal statement) survives humanization unchanged in meaning |
| Named-entity preservation | **Missing** — no test includes a proper noun and asserts it is byte-identical pre/post |
| Date preservation | **Missing** |
| Number preservation | **Missing** |
| Direct quote preservation | **Missing** — no test includes quoted dialogue/citation text and confirms quote marks and content survive |
| Uncertainty-language preservation | **Missing, and this is the audit's key gap** — no test exists for the exact P24 risk identified in §2/§4; existing `TestP24Hedging` tests only confirm the trigger phrase is *removed*, never that meaning is preserved |
| Schema preservation | **Present, but indirect** — `TestJSONStructureUnchanged` proves dict/list shape is untouched; no test calls `hypogeum.schemas.validate("script_package", ...)` on a post-humanization package to confirm it still passes full schema validation (as opposed to just "keys look the same") |
| Narration-only scope | **Present, implicitly** — `TestHumanizeBeats::test_structure_fields_unchanged` proves non-text fields are untouched, but no test explicitly asserts visual-direction fields elsewhere in `script_package` are never touched |
| Visual-direction isolation | **Missing** — no test includes a `script_package` with visual-direction-bearing fields and confirms they're absent from any humanizer call path |
| Repeated pipeline execution | **Missing** |
| Checkpoint resume | **Missing — this is the single most important gap given §1.6/§1.7's confirmed finding.** No test exercises `pipeline.py`'s `_run_script_engine` checkpoint-skip branch and confirms (or, as this audit found, disconfirms) that humanization survives a resume |
| Empty/null fields | **Partial** — `TestEdgeCases` covers empty string and whitespace-only; `TestHumanizeBeats::test_entry_without_text_skipped` and `test_non_string_text_skipped` cover missing/wrong-type `text` keys. No explicit `None` (Python `null`) case is tested (`test_none_not_passed`'s own docstring admits it "test[s] that empty string returns correctly" rather than testing `None`). |
| Arabic text | **Missing** — no non-Latin-script test exists anywhere in the file |
| Mixed Arabic/English text | **Missing** |
| Punctuation (beyond dashes) | **Missing** — no test covers semicolons, colons, ellipses, or other punctuation the regex patterns' surrounding context (e.g. `_RE_SPACE_COMMA`, `_RE_LEADING_COMMA`) could interact with unexpectedly |
| Em dashes | **Present** — the most thoroughly tested single concern in the file (6 dedicated tests) |
| Lists | **Missing** — no test includes markdown-style or comma-separated list content |
| Citations | **Missing** — no test includes a citation-like string (e.g. "(Smith, 2019)") and confirms it survives |
| Malformed inputs | **Partial** — non-dict beat entries and non-string `text` values are covered (`TestHumanizeBeats`); no test covers deeply nested or unexpectedly-typed `script_package` structures beyond the beats list itself |
| Regression against real documentary narration samples | **Missing entirely** — every test uses short, hand-constructed example sentences; none use actual `.hypogeum-assets/episodes/*/script_package_*.json` content from a real production run |

---

## 6. Migration Options

### Option A — Keep existing deterministic module only (no upstream runtime use)

- **Architecture:** No change. `hypogeum/humanizer.py` continues as-is; the pinned upstream skill remains installed but never wired to any agent.
- **Benefits:** Zero new risk, zero implementation cost, zero token/latency cost.
- **Risks:** Leaves the confirmed checkpoint-resume gap (§1.6/§1.7) and the P24 hedging-deletion risk (§2/§4) unaddressed indefinitely; leaves 18/33 upstream patterns (those requiring judgment) permanently unaddressed since deterministic regex cannot express them.
- **Token cost:** None. **Latency:** None. **Factual safety:** Same as today (medium-risk P24 gap persists).
- **Operational complexity:** None — status quo. **Rollback:** N/A. **Observability:** Same gaps as today.
- **Fit with autonomous runtime:** Perfect — already integrated, already unattended-safe in mechanism (though not in coverage).
- **Duplicate behavior:** N/A. **Upstream file remains verbatim:** Trivially yes, since it's never invoked. **Wrapper required:** No.

### Option B — Replace existing module with upstream skill through an LLM adapter (remove/disable deterministic regex after migration)

- **Architecture:** `hypogeum/humanizer.py` is deprecated/removed; a new LLM-calling Humanization capability (per the already-approved `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §4) replaces it entirely, invoking the upstream Humanizer skill's full 33-pattern judgment via an LLM call.
- **Benefits:** Full upstream coverage (all 33 patterns, including the 18 requiring judgment that no regex module could ever cover); resolves the checkpoint gap naturally if the new capability is wired the same way other schema-validated pipeline stages are (i.e., persisted to disk as part of its own output, not as an unpersisted post-processing mutation).
- **Risks:** Highest of all options — an LLM rewrite pass on narration text is exactly the kind of step that can alter facts, drop specifics, or change register unpredictably (the pinned `SKILL.md` itself warns about this: "Rewrite, don't delete... cover everything the original covers"). Removing the deterministic module also removes its zero-cost, zero-latency, zero-ambiguity guarantee for the patterns it *does* correctly handle. Directly conflicts with the correction-instructions' repeated caution against unattended LLM prose rewriting without a preservation contract.
- **Token cost:** Real, per-episode, proportional to narration length. **Latency:** Real, an added LLM round-trip per episode.
- **Factual safety:** Requires a full preservation contract (§8) and cannot be safely activated without it — this is the highest-stakes option for factual drift.
- **Operational complexity:** High — new failure modes (LLM call failure, truncation, content-filter rejection) not present in the deterministic path.
- **Rollback:** Harder — removing the deterministic module means there's no fallback if the LLM path is disabled; a true rollback would require restoring the deleted module.
- **Observability:** Requires new logging entirely (no equivalent exists today for an LLM-based text-editing step).
- **Fit with autonomous runtime:** Requires the same unattended-adaptation work already flagged in `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §4 (upstream skill is conversational/interactive by design).
- **Duplicate behavior:** No, since the deterministic module is removed. **Upstream file remains verbatim:** Yes, if the adapter composes it via `compose_system_prompt()` without editing it. **Wrapper required:** Yes, substantial.

### Option C — Deterministic pre-clean followed by upstream LLM humanization (both stages, non-overlapping responsibilities)

- **Architecture:** `hypogeum/humanizer.py` runs first (as today, its 6 groups), producing text with the easy, unambiguous patterns already removed; the upstream-derived LLM capability then runs second, scoped explicitly to the 18 judgment-requiring patterns the deterministic pass cannot reach, with instructions to *skip* patterns already handled deterministically (avoiding redundant work, not redundant risk, since the deterministic pass is idempotent).
- **Benefits:** Combines the deterministic pass's zero-risk certainty for its 4–5 solid patterns with the LLM pass's ability to cover the 18 judgment-only patterns; the LLM pass has less work to do per episode (a smaller effective diff), which may reduce drift risk versus Option B's single large rewrite.
- **Risks:** Two-stage pipelines have more surface area for the checkpoint-resume bug class identified in §1.6 to recur if not designed carefully from the start — this option must explicitly define whether the LLM stage's checkpoint file captures pre- or post-deterministic-pass text (learning directly from §1's finding).
- **Token cost:** Same as Option B for the LLM stage (still per-episode, still proportional to narration length) — not reduced, since the LLM pass must still read the full text even if fewer edits are expected.
- **Latency:** Same as Option B, plus the (negligible) deterministic pass.
- **Factual safety:** Better than Option B in principle (smaller LLM edit surface), but still requires the full §8 preservation contract for the LLM stage.
- **Operational complexity:** Higher than either A or B alone — two stages to test, monitor, and version independently.
- **Rollback:** Each stage can be independently disabled, which is an operational advantage over Option B.
- **Observability:** Requires two sets of log events, one per stage.
- **Fit with autonomous runtime:** Same adaptation requirement as Option B.
- **Duplicate behavior:** Explicitly avoided by design if scoped correctly; the risk is scope leakage (the LLM stage re-doing what the deterministic stage already did) if not carefully bounded.
- **Upstream file remains verbatim:** Yes. **Wrapper required:** Yes, for both the deterministic-pass integration point and the LLM adapter.

### Option D — Upstream LLM humanization only for flagged scripts (conditional quality-gate route)

- **Architecture:** A quality gate (not yet existing — would need its own design) scores narration for AI-tell density; only scripts above a threshold route through the upstream LLM humanization pass, others pass through with only the existing deterministic module (or untouched).
- **Benefits:** Minimizes token/latency cost by only paying for LLM humanization when it's likely to matter; a natural complement to the fact that Script Engine's own prompt is already tuned to reduce AI-tells at the source.
- **Risks:** Requires building and validating a scoring mechanism, which is itself a new component with its own accuracy risk (a scorer that misses a genuinely AI-sounding script provides false confidence); adds a decision-point failure mode not present in any other option.
- **Token cost:** Lower average cost than B/C (conditional), but requires the scoring pass itself (likely also an LLM call, or a cheaper heuristic — unscoped by this audit).
- **Latency:** Variable, conditional on the gate's decision.
- **Factual safety:** Same requirements as B/C when the LLM path fires; the deterministic-only path (when the gate doesn't fire) retains today's P24 risk.
- **Operational complexity:** Highest of all options — requires designing, validating, and maintaining the scoring mechanism itself, which is out of scope for anything verified in this audit.
- **Rollback:** The gate itself can be disabled (fail-safe to "always deterministic-only" or "always LLM"), which is an advantage.
- **Observability:** Requires logging the gate's decision and score, in addition to whichever path fires.
- **Fit with autonomous runtime:** Feasible, but the scoring mechanism's own reliability is unverified and would need its own audit.
- **Duplicate behavior:** Avoided by design (mutually exclusive routing). **Upstream file remains verbatim:** Yes. **Wrapper required:** Yes, plus a new scoring component not evaluated by this audit.

### Option E — Humanization as a separate post-Script-Engine agent/capability (schema-preserving handoff before Audio Agent)

- **Architecture:** Matches the placement already approved in `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §4 — a distinct capability/stage between Script Engine and Audio Agent, with its own schema-validated input/output contract (rather than an inline post-processing mutation inside `run_script_engine()` as today's deterministic module is). Could use the deterministic module, the upstream LLM skill, or both (i.e., this option is orthogonal to A–D on the "what runs" axis; it specifically fixes the "where it runs and how it's persisted" axis).
- **Benefits:** **Directly resolves the checkpoint-resume gap (§1.6/§1.7) by construction** — if humanization is its own pipeline stage with its own checkpoint file (`humanized_script_package_{episode_id}.json` or similar), then a resume of *that* stage re-runs humanization exactly like every other stage's checkpoint already correctly does, and a resume of an *earlier* stage naturally re-triggers this stage since its own checkpoint wouldn't exist yet. This is the only option among A–E that structurally fixes the confirmed integration risk rather than working around it.
- **Risks:** Requires a new schema (`humanized_script_package` or an in-place schema-compatible re-validation of `script_package`), a new pipeline stage entry, and careful definition of exactly which fields it may touch (§8) — non-trivial design work, though well-scoped.
- **Token cost:** Depends on which mechanism (A–D) is chosen for "what runs" within this stage; the placement itself adds no token cost.
- **Latency:** One additional pipeline stage boundary (checkpoint write/read), negligible compared to any LLM call it might contain.
- **Factual safety:** Best positioned of all options to enforce a preservation contract, since it has its own dedicated schema-validation boundary rather than being an untracked mutation inside another agent's function body.
- **Operational complexity:** Moderate — a new, well-defined pipeline stage is simpler to reason about than an inline side-effect buried inside `script_engine.py`, which is itself a form of complexity reduction versus today's architecture.
- **Rollback:** Clean — disabling the stage (skip it) leaves `script_package` exactly as Script Engine produced it, with no ambiguity about partial application.
- **Observability:** A dedicated stage gets its own `stage_started`/`stage_completed` log events for free, matching every other pipeline stage's existing pattern — directly fixes §4's observability-gap finding.
- **Fit with autonomous runtime:** Best fit — matches the existing checkpoint/resume/schema-validation pattern every other stage already uses correctly, rather than introducing a new, inconsistent side-effect pattern.
- **Duplicate behavior:** N/A — this option is about placement, not mechanism. **Upstream file remains verbatim:** Yes, if a wrapper is used per Installation Policy. **Wrapper required:** Yes, regardless of which mechanism (A–D) runs inside this stage.

---

## 7. Recommended Target Architecture

**Recommended: Option E (separate post-Script-Engine capability) as the structural fix, running Option C's mechanism (deterministic pre-clean + upstream LLM pass) inside it — not implemented by this document.**

Rationale: Option E is the only option that structurally resolves the confirmed checkpoint-resume gap (§1.6/§1.7), which is this audit's most severe, concrete finding — independent of which humanization *mechanism* is chosen. Option C is recommended as the mechanism because it preserves the existing deterministic module's zero-risk, zero-cost handling of the 4–5 patterns it already covers correctly (avoiding Option B's regression risk of re-litigating already-solved patterns through a less predictable LLM pass), while adding upstream LLM coverage only for the 18 patterns genuinely requiring judgment that no deterministic module could ever reach.

- **`hypogeum/humanizer.py`:** Remains, relocated in call order to execute inside the new stage rather than inline inside `script_engine.py` — not removed, not deprecated. Its P24 hedging-deletion behavior (§2, §3) should be corrected to soften rather than delete (matching upstream's own §24 example) as part of this migration, since that specific divergence from upstream's demonstrated behavior is the audit's clearest concrete risk finding — but this is a content fix to an existing module, not new architecture, and is itself out of scope for implementation by this document.
- **Deterministic transformations:** Remain, as the first sub-stage within the new capability.
- **Upstream Humanizer:** Loaded into the new separate capability (not Script Engine directly), via `compose_system_prompt()` per the existing internal-skill pattern, per Installation Policy (verbatim, thin wrapper).
- **Exact position in the pipeline:** Between Script Engine and Audio Agent — `... → Script Engine → [NEW: Humanization Capability] → Audio Agent → ...`, exactly as already specified in `SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §4.
- **Exact input fields:** `script_package["sections"][].narration_beats[].text`, `script_package["shorts_scripts"][].narration_beats[].text` only — the same scope `humanize_beats()` already correctly restricts itself to.
- **Exact output fields:** The same fields, in place, within a schema-revalidated `script_package`.
- **Fields that must never be modified:** `beat_id`, `beat_type`, `section_id`, `title`, `short_id`, `text_overlays` (structure and `beat_reference`), `chapter_markers`, `script_integrity`, `traceability_manifest`, `episode_metadata` — all confirmed untouched by the existing module's structural tests (§5) and must remain so by contract, not just by accident of current regex scope.
- **Preservation contract:** See §8.
- **Idempotency contract:** The stage must be safely re-runnable — calling it twice on already-humanized text (deterministic sub-stage) must produce no further change (already true per `TestIdempotent`); the LLM sub-stage's idempotency is unverified and must be established by a new test before activation (§9).
- **Retry/checkpoint behavior:** Must write its own checkpoint file after completion, matching every other pipeline stage's pattern, so that a resume of this specific stage re-runs humanization rather than silently loading a pre-humanization artifact — this is the specific mechanism that resolves §1.6/§1.7's gap.
- **Model/provider requirement:** Unscoped by this audit — a separate decision, likely the same OpenRouter Sonnet family already used by Script Engine, but not determined here.
- **Token-budget ceiling:** Unscoped by this audit — should be bounded relative to narration length, per the same discipline already established for VPD Stage B's B1 token-budget work.
- **Fallback behavior:** If the LLM sub-stage fails, the deterministic sub-stage's output (already applied first) should still flow downstream rather than blocking the pipeline — unscoped in full detail, flagged as a required design decision.
- **Failure behavior:** Unscoped — should follow the same `SchemaError`/`AgentError` propagation pattern every other agent already uses.
- **Observability events:** A `humanization_stage_started`/`humanization_stage_completed` pair, plus an explicit field distinguishing "deterministic-only" vs. "deterministic + LLM" application per episode, directly closing §4's observability gap.
- **Activation flag:** Should exist (e.g. a config flag gating the LLM sub-stage independently from the deterministic sub-stage, which should always run) — unscoped in exact naming by this audit.
- **Rollback path:** Disable the stage entirely (skip it) to fully revert to raw Script Engine output; disable only the LLM sub-stage to fall back to today's deterministic-only behavior, corrected for the checkpoint gap.

**None of the above is implemented by this document.**

---

## 8. Required Preservation Contract

The future Humanization capability must guarantee preservation of the following, unchanged, byte-for-byte where noted:

| Field/concept | Preservation requirement |
|---|---|
| Episode ID | Unchanged (not a text field the humanizer ever touches) |
| Beat IDs | Unchanged, byte-identical (already true structurally per `TestHumanizeBeats`/`TestJSONStructureUnchanged`; must extend to the LLM sub-stage) |
| Scene references | Unchanged — not a Script Engine field, but any cross-reference Script Engine emits toward VPD must survive |
| Verified facts | Meaning-preserved — no factual claim's truth value may change; this requires a semantic diff check, not merely a string check |
| Names | Byte-identical — proper nouns (people, places, artifacts) must not be paraphrased, abbreviated, or substituted |
| Dates | Byte-identical |
| Numbers | Byte-identical, including units |
| Locations | Byte-identical |
| Direct quotations | Byte-identical, including internal punctuation — quote marks and the quoted content itself must never be edited by a "rewrite" pass, only the surrounding prose may change |
| Citations | Byte-identical |
| Evidence levels | Preserved — if Research Director tagged a claim's confidence, Script Engine's phrasing of that confidence must not be silently strengthened or weakened by humanization (directly addresses the P24 finding in §2/§4) |
| Uncertainty language | Preserved in kind, not necessarily in exact phrasing — a hedge may be *reworded* (matching upstream's own §24 example: "could potentially possibly" → "may") but must never be *deleted* such that a qualified claim becomes unqualified |
| Prohibited claims | N/A to this stage directly — inherited from upstream Research Director/Script Engine's own constraints, humanization must not introduce a prohibited claim that wasn't already present |
| Narrative order | Unchanged — beat sequence within a section must not be reordered |
| Timing/duration fields | Unchanged — not text fields the humanizer touches, but must be confirmed untouched by the new stage's schema re-validation |
| Schema shape | Unchanged — the new stage must call `hypogeum.schemas.validate("script_package", ...)` again after its own mutation, closing the gap noted in §1.4 where today's inline mutation is never re-validated |
| Visual directions | Unchanged — confirmed today that no humanizer call site touches these; the new stage's field scope (§7) must maintain this exclusion explicitly, not by accident of not being wired there |
| Metadata | Unchanged except where `hypogeum/publishing/metadata.py`'s existing, separate humanization call sites are concerned — this audit does not recommend consolidating those into the new stage without a separate decision, since they operate on different data (`metadata_source`, not `script_package`) at a different pipeline point (publish time, not script-generation time) |

**Fields Humanizer may edit:** exclusively the `text` string value within each `narration_beats[]` entry (both `sections[].narration_beats[]` and `shorts_scripts[].narration_beats[]`) — no other field, at any nesting level, in `script_package`.

---

## 9. Required Future Test Matrix

| Category | Tests required |
|---|---|
| **Unit** | Coverage for the remaining untested pattern instances within each existing group (e.g. the 3 of 8 P01 phrases, 4 of 10 P07 words, 5 of 9 P20 markers, 2 of 7 P23 phrases, 3 of 6 P24 patterns not currently sampled by name) |
| **Schema** | `validate("script_package", package)` called explicitly *after* the new stage's mutation (deterministic and, once designed, LLM), not merely inferred from key-set equality |
| **Preservation** | One test per row of §8's table — named entities, dates, numbers, locations, direct quotations, citations, evidence-level phrasing, uncertainty-language kind-preservation (specifically: a hedge is reworded, never deleted) |
| **Idempotency** | Full-stage idempotency: invoke the new capability twice on the same input, assert identical output; distinct from today's string-level-only `TestIdempotent` |
| **Integration** | `run_script_engine()` (or its successor stage) invoked end-to-end with a mocked `run_agent()` LLM response, confirming humanization is applied to the actual returned object flowing to the next pipeline stage |
| **Checkpoint** | Exercise `pipeline.py`'s checkpoint-skip branch for the new stage specifically, confirming a resume re-applies (or correctly re-loads already-applied) humanization — the direct regression test for this audit's central finding (§1.6/§1.7) |
| **Rollback** | Disabling the new stage (or its LLM sub-stage specifically) restores exactly today's deterministic-only behavior, with no partial-application state |
| **Token-budget** | Once a ceiling is set (§7), a test confirming the LLM sub-stage's prompt size stays within it for a representative narration length |
| **Real-sample regression** | At least one test using an actual `.hypogeum-assets/episodes/*/script_package_*.json` from a completed production run (not a hand-constructed example sentence), confirming the full pipeline produces plausible, fact-preserving output on real documentary narration — closing the gap identified in §5 |

---

## 10. Final Verdict

| Item | Status |
|---|---|
| Existing deterministic Humanizer (`hypogeum/humanizer.py`) | **KEEP** — remains in the recommended architecture (§7), relocated but not removed or deprecated; its P24 sub-behavior requires a content fix (soften, not delete) as a separate, small, future task |
| Pinned upstream Humanizer (`third_party/skills/humanizer/...`) | **ADOPT** — already installed (prior task), confirmed suitable, no license or content blocker found |
| Direct wiring into Script Engine (i.e., today's inline pattern, or wiring the LLM skill the same way) | **DO NOT ADOPT** — the inline-mutation pattern is exactly what produced the confirmed checkpoint-resume gap (§1.6/§1.7); any future wiring must not repeat this pattern |
| Separate Humanization capability (Option E placement) | **READY FOR DESIGN** — the architectural direction is clear and justified by this audit's findings, but exact schema, stage boundaries, and activation flag naming are not yet designed |
| Double-stage humanization (Option C mechanism: deterministic + upstream LLM) | **READY FOR DESIGN** — recommended mechanism, not yet specified in implementable detail (model, token ceiling, fallback behavior all unscoped per §7) |
| Production activation | **NOT READY** — reason, explicitly: the checkpoint-resume gap (§1.6/§1.7) is a live, confirmed defect in the currently-active deterministic path, the P24 hedging-deletion risk is unmitigated, and no preservation-contract test (§8/§9) exists yet for any humanization mechanism, deterministic or LLM. None of these are estimation — all three are demonstrated facts from direct code/test inspection in this audit. |

`BLOCKED` is not used anywhere above — "NOT READY" is used for production activation specifically because the blocking conditions are named explicitly (checkpoint gap, P24 risk, missing preservation tests), matching this repository's manifest convention (`UPSTREAM_SKILL_MANIFEST.md` §2) of never using an unexplained blocking term.

---

## 11. Next Smallest Safe Step

**Design (not implement) the new Humanization pipeline stage's exact schema and checkpoint contract** — specifically: define the checkpoint file name/path, confirm whether it re-uses the `script_package` schema or introduces a new one, and specify the exact log event names for §7's observability requirement. This is a design task, not a code change, and directly targets this audit's single highest-severity finding (the checkpoint-resume gap) without touching any existing file. Not executed by this document.

No implementation is authorized by this document.
