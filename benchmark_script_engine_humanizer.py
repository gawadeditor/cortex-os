"""
benchmark_script_engine_humanizer.py — Prompt Validation Phase: Script Engine
Humanizer A/B/C benchmark.

Compares three conditions across 4 trials (1 synthetic + 3 real episodes) using
identical narrative_package inputs:

  A — Current production : Humanizer skill in system prompt + humanize_beats() regex
  B — Baseline            : no Humanizer skill in system prompt + humanize_beats() regex
  C — Skill-only (pure)   : Humanizer skill in system prompt, NO humanize_beats() regex

Read-only with respect to production code: hypogeum/agents/script_engine.py and
hypogeum/humanizer.py are imported and called, never modified. Every condition is
produced by calling the same two production functions (run_agent(), humanize_beats())
that hypogeum.agents.script_engine.run_script_engine() itself calls internally, in
the same order it calls them — this harness never patches or edits either module,
and Condition A produces byte-identical output to calling run_script_engine()
directly. Building it this way (rather than calling run_script_engine() for A)
lets the harness also capture A's raw, pre-regex text for the isolated-skill-effect
metrics, symmetric with B and C.

Makes real OpenRouter/Anthropic API calls (approved spend — see
docs/architecture/SCRIPT_ENGINE_HUMANIZER_VALIDATION_REPORT.md for the protocol).

Usage:
    python3 benchmark_script_engine_humanizer.py
    python3 benchmark_script_engine_humanizer.py --trials synthetic ep_001
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hypogeum.agent import run_agent
from hypogeum.agents import script_engine
from hypogeum.config import Config
from hypogeum.humanizer import (
    _FILLER_PATTERNS,
    _INFLATION_PATTERNS,
    _VOCABULARY_PATTERNS,
    humanize_beats,
)
from hypogeum.prompt_loader import compose_system_prompt
from hypogeum.schemas import SchemaError, validate

ASSETS_ROOT = Path(__file__).resolve().parent / ".hypogeum-assets" / "episodes"

REAL_TRIALS = ["ep_001", "ep_002", "ep_003"]
ALL_TRIALS = ["synthetic"] + REAL_TRIALS


# ---------------------------------------------------------------------------
# Trial inputs
# ---------------------------------------------------------------------------

def _synthetic_narrative_package() -> dict:
    """
    Mirrors tests/test_script_engine.py::_make_narrative_package() — copied
    here (not imported from the test module) so this benchmark has no
    dependency on test-module import-time side effects. Same content.
    """
    return {
        "episode_metadata": {
            "topic": "The Antikythera Mechanism",
            "archetype": "default_six_section",
            "central_question": "Who built the Antikythera Mechanism?",
            "central_answer": "Likely Hipparchus or his school.",
            "episode_id": "ep_synthetic",
            "narrative_model": "narrative_architect_v1",
        },
        "episode_structure": {
            "archetype": "default_six_section",
            "sections": [
                {"section_id": "s_001", "title": "Hook", "narrative_purpose": "Open the question.", "entity_count": 1, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "high"},
                {"section_id": "s_002", "title": "Context", "narrative_purpose": "Establish setting.", "entity_count": 3, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "medium"},
                {"section_id": "s_003", "title": "Rising Mystery", "narrative_purpose": "First strangeness.", "entity_count": 2, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "medium"},
                {"section_id": "s_004", "title": "Escalation", "narrative_purpose": "Deepen mystery.", "entity_count": 3, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "high"},
                {"section_id": "s_005", "title": "Revelation", "narrative_purpose": "Land the reveal.", "entity_count": 2, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "high"},
                {"section_id": "s_006", "title": "Resolution", "narrative_purpose": "Elevate mystery.", "entity_count": 1, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "low"},
            ],
        },
        "narrative_arc": {
            "setup": "Establish the wreck and the object's strangeness.",
            "escalation": "Each research line deepens the impossibility.",
            "revelation": "The mechanism maps the entire known cosmos.",
            "resolution": "We still don't know how they knew.",
        },
        "reveal_architecture": {
            "reveal_map": [{"reveal_id": "rev_001", "section_id": "s_005", "beat_position": 0, "claim_id": "clm_001", "reveal_type": "central", "impact_justification": "Answers the central question.", "adjacent_reveal_exception": None}],
            "viewer_question_state": [{"after_section_id": "s_001", "question": "What is this object?"}],
            "open_question_tracker": [],
        },
        "shorts_selection": [
            {"short_id": "short_001", "title": "The Gears", "source_claim_id": "clm_001", "visual_anchor_signal": "Bronze gears", "spoiler_policy": "no_central_answer", "narrative_arc": "Discovery to wonder.", "text_overlay_plan": {"opening_hook": {"beat_position": 0, "narrative_intent": "Create curiosity."}, "key_information": [{"beat_position": 1, "narrative_intent": "State key fact."}], "payoff": {"beat_position": 2, "narrative_intent": "Land wonder."}}},
            {"short_id": "short_002", "title": "The Wreck", "source_claim_id": "clm_001", "visual_anchor_signal": "Shipwreck site", "spoiler_policy": "no_central_answer", "narrative_arc": "From discovery to mystery.", "text_overlay_plan": {"opening_hook": {"beat_position": 0, "narrative_intent": "Hook on the wreck."}, "key_information": [], "payoff": {"beat_position": 1, "narrative_intent": "Elevate mystery."}}},
            {"short_id": "short_003", "title": "The Date", "source_claim_id": "clm_001", "visual_anchor_signal": "Timeline graphic", "spoiler_policy": "no_central_answer", "narrative_arc": "From date to implications.", "text_overlay_plan": {"opening_hook": {"beat_position": 0, "narrative_intent": "Shock with the date."}, "key_information": [], "payoff": {"beat_position": 1, "narrative_intent": "Why it matters."}}},
        ],
        "narrative_signals": [],
        "differentiation_record": {"ep_hypothesis": "The mechanism's social role.", "authority_tier": "refinement", "na_angle": "Focus on who used it.", "adjustment_rationale": "Research supports this.", "replacement_rationale": None},
        "narrative_integrity_log": [],
        "traceability": {"total_claims_used": 1, "total_reveals": 1, "total_open_questions": 0, "research_coverage_warning": False, "claim_ids_used": ["clm_001"]},
    }


def _load_narrative_package(trial_id: str) -> dict:
    if trial_id == "synthetic":
        return _synthetic_narrative_package()
    path = ASSETS_ROOT / trial_id / f"narrative_package_{trial_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Deterministic metrics
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z']+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "is", "was", "were", "are", "that", "this", "it", "as", "by",
    "from", "be", "has", "have", "had", "its", "their", "he", "she", "they",
    "we", "you", "not", "no",
}
_SENT_RE = re.compile(r"[.!?]+(?:\s+|$)")
_BE_FORMS = r"\b(?:is|are|was|were|be|been|being)\b"
_PASSIVE_RE = re.compile(_BE_FORMS + r"\s+\w+(?:ed|en)\b", re.IGNORECASE)
_VOWEL_GROUPS_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)

# Deliberately excludes any term/phrase already covered by humanizer.py's own
# P01 (_INFLATION_PATTERNS) or P07 (_VOCABULARY_PATTERNS) tables — e.g. "testament
# to", "seamless(ly)", "pivotal", "stands as a" are already matched there (in a
# more specific form); repeating them here would double-count the same phrase.
_AI_CLICHE_EXTRA = [
    r"\btapestry\b", r"\bunderscores?\b", r"\bboasts?\b",
    r"\bunwavering\b", r"\bmyriad\b", r"\bat its core\b", r"\bplethora\b",
    r"\bnavigate\b", r"\bembark\b", r"\bunveil(?:s|ed|ing)?\b",
    r"\belevate(?:s|d)?\b", r"\brobust\b",
    r"\brealm of\b", r"\blandscape of\b", r"\bin essence\b",
]

_EMBELLISHMENT_TERMS = [
    r"\bunprecedented\b", r"\bnever before\b", r"\bimpossible\b",
    r"\bshocking\b", r"\bincredible\b", r"\bmost extraordinary\b",
    r"\bgreatest\b", r"\bunbelievable\b", r"\bmind-blowing\b",
    r"\bstunning\b", r"\bastonishing\b",
]


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split(text) if s.strip()]


def _extract_beat_texts(script_package: dict) -> list[str]:
    texts: list[str] = []
    for sec in script_package.get("sections", []):
        for beat in sec.get("narration_beats", []):
            t = beat.get("text", "")
            if isinstance(t, str) and t.strip():
                texts.append(t)
    for short in script_package.get("shorts_scripts", []):
        for beat in short.get("narration_beats", []):
            t = beat.get("text", "")
            if isinstance(t, str) and t.strip():
                texts.append(t)
    return texts


def _full_text(script_package: dict) -> str:
    return " ".join(_extract_beat_texts(script_package))


def repeated_ngrams(text: str, n: int) -> int:
    words = _tokenize(text)
    grams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    grams = [g for g in grams if not all(w in _STOPWORDS for w in g)]
    counts = Counter(grams)
    return sum(1 for _, c in counts.items() if c > 1)


def sentence_length_stats(text: str) -> dict:
    sents = _sentences(text)
    lengths = [len(_tokenize(s)) for s in sents if _tokenize(s)]
    if not lengths:
        return {"mean": 0.0, "stdev": 0.0, "count": 0}
    return {
        "mean": round(statistics.mean(lengths), 2),
        "stdev": round(statistics.pstdev(lengths), 2) if len(lengths) > 1 else 0.0,
        "count": len(lengths),
    }


def mattr(text: str, window: int = 50) -> float:
    words = _tokenize(text)
    if not words:
        return 0.0
    if len(words) < window:
        return round(len(set(words)) / len(words), 4)
    ratios = [
        len(set(words[i:i + window])) / window
        for i in range(0, len(words) - window + 1)
    ]
    return round(statistics.mean(ratios), 4)


def raw_ttr(text: str) -> float:
    words = _tokenize(text)
    return round(len(set(words)) / len(words), 4) if words else 0.0


def _count_pattern_hits(text: str, patterns) -> int:
    count = 0
    for item in patterns:
        pat = item[0] if isinstance(item, tuple) else item
        count += len(re.findall(pat, text, re.IGNORECASE))
    return count


def filler_phrase_count(text: str) -> int:
    return _count_pattern_hits(text, _FILLER_PATTERNS)


def ai_cliche_count(text: str) -> int:
    return (
        _count_pattern_hits(text, _INFLATION_PATTERNS)
        + _count_pattern_hits(text, _VOCABULARY_PATTERNS)
        + _count_pattern_hits(text, _AI_CLICHE_EXTRA)
    )


def passive_voice_frequency(text: str) -> float:
    sents = _sentences(text)
    if not sents:
        return 0.0
    return round(len(_PASSIVE_RE.findall(text)) / len(sents), 4)


def _count_syllables(word: str) -> int:
    groups = _VOWEL_GROUPS_RE.findall(word.lower())
    count = len(groups)
    if word.lower().endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def flesch_reading_ease(text: str) -> float:
    words = _tokenize(text)
    sents = _sentences(text)
    if not words or not sents:
        return 0.0
    syllables = sum(_count_syllables(w) for w in words)
    score = 206.835 - 1.015 * (len(words) / len(sents)) - 84.6 * (syllables / len(words))
    return round(score, 2)


def embellishment_count(text: str) -> int:
    return sum(len(re.findall(p, text, re.IGNORECASE)) for p in _EMBELLISHMENT_TERMS)


def extract_key_facts(narrative_package: dict) -> list[str]:
    facts: list[str] = []
    em = narrative_package.get("episode_metadata", {})
    for k in ("central_question", "central_answer", "topic"):
        v = em.get(k)
        if isinstance(v, str) and v:
            facts.append(v)
    arc = narrative_package.get("narrative_arc", {})
    for k in ("setup", "escalation", "revelation", "resolution"):
        v = arc.get(k)
        if isinstance(v, str) and v:
            facts.append(v)
    return facts


def _key_terms(fact: str) -> list[str]:
    return [w for w in _tokenize(fact) if len(w) > 4 and w not in _STOPWORDS]


def factual_coverage(narrative_package: dict, output_text: str) -> dict:
    facts = extract_key_facts(narrative_package)
    output_words = set(_tokenize(output_text))
    covered = 0
    detail = []
    for fact in facts:
        terms = _key_terms(fact)
        if not terms:
            continue
        hits = sum(1 for t in terms if t in output_words)
        ratio = hits / len(terms)
        if ratio >= 0.3:
            covered += 1
        detail.append({"fact": fact[:80], "term_coverage_ratio": round(ratio, 2)})
    return {"facts_checked": len(detail), "facts_covered": covered, "detail": detail}


def cta_compliance(script_package: dict) -> dict:
    si = script_package.get("script_integrity", {})
    return {
        "cta_count": si.get("cta_count"),
        "cta_count_ok": si.get("cta_count") == 2,
        "aggressive_cta_language": si.get("aggressive_cta_language"),
        "aggressive_cta_ok": si.get("aggressive_cta_language") is False,
    }


def schema_check(script_package: dict) -> dict:
    try:
        validate("script_package", script_package)
        return {"passed": True, "error": None}
    except SchemaError as exc:
        return {"passed": False, "error": f"{exc.path}: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "error": f"unexpected: {exc}"}


def compute_metrics(script_package: dict, narrative_package: dict) -> dict:
    text = _full_text(script_package)
    words = _tokenize(text)
    return {
        "word_count": len(words),
        "beat_count": len(_extract_beat_texts(script_package)),
        "repeated_3grams": repeated_ngrams(text, 3),
        "repeated_4grams": repeated_ngrams(text, 4),
        "repeated_5grams": repeated_ngrams(text, 5),
        "sentence_length": sentence_length_stats(text),
        "lexical_diversity_mattr": mattr(text),
        "lexical_diversity_raw_ttr": raw_ttr(text),
        "filler_phrase_count": filler_phrase_count(text),
        "ai_cliche_count": ai_cliche_count(text),
        "passive_voice_frequency": passive_voice_frequency(text),
        "flesch_reading_ease": flesch_reading_ease(text),
        "embellishment_count": embellishment_count(text),
        "factual_coverage": factual_coverage(narrative_package, text),
        "cta_compliance": cta_compliance(script_package),
        "schema": schema_check(script_package),
    }


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

def _run_condition(
    *,
    label: str,
    system_prompt: str,
    narrative_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    apply_regex: bool,
) -> dict:
    """
    Run one condition: a single run_agent() call using the given system_prompt,
    optionally followed by the same humanize_beats() post-processing
    hypogeum.agents.script_engine.run_script_engine() applies. Returns a dict
    with "ok", "raw" (pre-regex script_package), "final" (post-regex, or same
    as raw if apply_regex is False), and "error".
    """
    print(f"    [{label}] calling run_agent()...")
    try:
        raw_package = run_agent(
            agent_name=f"{script_engine._AGENT_NAME}_bench_{label.lower()}",
            model_slug=script_engine._CLAUDE_SONNET,
            system_prompt=system_prompt,
            input_package={"task": "write_script", "narrative_package": narrative_package},
            output_schema="script_package",
            episode_id=episode_id,
            max_tokens=script_engine._SE_MAX_TOKENS,
            config=config,
            output_dir=output_dir,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"      FAILED: {exc}")
        return {"ok": False, "raw": None, "final": None, "error": str(exc)}

    final_package = copy.deepcopy(raw_package)
    if apply_regex:
        for section in final_package.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        for short in final_package.get("shorts_scripts", []):
            humanize_beats(short.get("narration_beats", []))

    print(f"      OK — {len(_extract_beat_texts(final_package))} beats")
    return {"ok": True, "raw": raw_package, "final": final_package, "error": None}


def run_trial(trial_id: str, config: Config) -> dict:
    print(f"\n{'─'*70}\n  TRIAL: {trial_id}\n{'─'*70}")
    narrative_package = _load_narrative_package(trial_id)
    episode_id = f"val_{trial_id}"
    trial_dir = ASSETS_ROOT / episode_id / "validation" / "humanizer_ab"

    baseline_prompt = compose_system_prompt(script_engine._PROMPT_FILE)

    conditions = {}
    conditions["A"] = _run_condition(
        label="A", system_prompt=script_engine.SYSTEM_PROMPT,
        narrative_package=narrative_package, config=config, episode_id=episode_id,
        output_dir=trial_dir / "condition_a", apply_regex=True,
    )
    conditions["B"] = _run_condition(
        label="B", system_prompt=baseline_prompt,
        narrative_package=narrative_package, config=config, episode_id=episode_id,
        output_dir=trial_dir / "condition_b", apply_regex=True,
    )
    conditions["C"] = _run_condition(
        label="C", system_prompt=script_engine.SYSTEM_PROMPT,
        narrative_package=narrative_package, config=config, episode_id=episode_id,
        output_dir=trial_dir / "condition_c", apply_regex=False,
    )

    metrics = {}
    for label, result in conditions.items():
        if not result["ok"]:
            metrics[label] = {"raw": None, "final": None, "error": result["error"]}
            continue
        metrics[label] = {
            "raw": compute_metrics(result["raw"], narrative_package),
            "final": compute_metrics(result["final"], narrative_package),
            "error": None,
        }

    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    _print_trial_summary(trial_id, metrics)

    return {"trial_id": trial_id, "conditions": conditions, "metrics": metrics}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_trial_summary(trial_id: str, metrics: dict) -> None:
    print(f"\n  Metrics summary (final, post-processing) — {trial_id}")
    header = f"  {'Metric':<28} {'A':<14} {'B':<14} {'C':<14}"
    print(header)
    print(f"  {'-'*27} {'-'*13} {'-'*13} {'-'*13}")
    rows = [
        ("word_count", lambda m: m["word_count"]),
        ("sentence stdev", lambda m: m["sentence_length"]["stdev"]),
        ("lexical MATTR", lambda m: m["lexical_diversity_mattr"]),
        ("filler_phrase_count", lambda m: m["filler_phrase_count"]),
        ("ai_cliche_count", lambda m: m["ai_cliche_count"]),
        ("passive_voice_freq", lambda m: m["passive_voice_frequency"]),
        ("flesch_reading_ease", lambda m: m["flesch_reading_ease"]),
        ("embellishment_count", lambda m: m["embellishment_count"]),
        ("repeated_4grams", lambda m: m["repeated_4grams"]),
        ("cta_count_ok", lambda m: m["cta_compliance"]["cta_count_ok"]),
        ("schema_pass", lambda m: m["schema"]["passed"]),
    ]
    for name, fn in rows:
        vals = []
        for label in ("A", "B", "C"):
            m = metrics[label]["final"]
            vals.append(str(fn(m)) if m else "ERROR")
        print(f"  {name:<28} {vals[0]:<14} {vals[1]:<14} {vals[2]:<14}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Script Engine Humanizer A/B/C benchmark")
    parser.add_argument("--trials", nargs="+", default=ALL_TRIALS, choices=ALL_TRIALS)
    args = parser.parse_args()

    try:
        config = Config()
    except Exception as exc:  # noqa: BLE001
        print(f"Config error: {exc}", file=sys.stderr)
        return 4

    print(f"\n{'='*70}")
    print(f"  SCRIPT ENGINE — HUMANIZER A/B/C BENCHMARK")
    print(f"  A: production (skill + regex)   B: baseline (no skill + regex)")
    print(f"  C: skill-only (skill, no regex)")
    print(f"  Trials: {args.trials}")
    print(f"{'='*70}")

    all_results = []
    for trial_id in args.trials:
        all_results.append(run_trial(trial_id, config))

    print(f"\n\n{'='*70}")
    print(f"  ALL TRIALS COMPLETE — {len(all_results)} trial(s)")
    print(f"  Metrics written under .hypogeum-assets/episodes/val_*/validation/humanizer_ab/")
    print(f"{'='*70}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
