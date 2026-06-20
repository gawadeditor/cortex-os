"""
hypogeum/agents/thumbnail_prompt_validator.py — Pre-generation prompt distinctness validator

Runs after thumbnail_prompt_writer returns the generation plan and before
MUAPI image generation begins. Validates that the three main_episode
ranked_candidates represent genuinely distinct image prompts.

Root cause being prevented: when primary_subject_identity was prepended to all
three prompts, the first 60+ words were identical across candidates, making
A/B/C testing meaningless. ep_003 demonstrated this failure: all three prompts
started with "Corroded bronze fragments with dark verdigris patina, exposed gear
teeth, Koine Greek inscriptions, warm amber directional lighting against dark
background" — producing three visually near-identical thumbnails.

Checks:
  1. Pairwise first-40-word Jaccard similarity (threshold 0.60 → fail)
  2. All three hook texts identical (string match) → fail
  3. All three image prompts share the same first 5 words → fail (opening collapse)

Public API:
    validate_prompt_distinctness(ranked_candidates) -> dict
        Returns {"passed": bool, "failures": list[str], "warnings": list[str]}

    PromptDistinctnessError — raised when validation fails (import from this module)
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class PromptDistinctnessError(ValueError):
    """Raised when three image prompts are too similar to support A/B/C testing."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__(
            f"Prompt distinctness validation failed ({len(failures)} issue(s)): "
            + "; ".join(failures)
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return text.lower().strip()


def _first_n_words(text: str, n: int = 40) -> list[str]:
    return _normalize(text).split()[:n]


def _word_trigrams(words: list[str]) -> set[tuple]:
    if len(words) < 3:
        return set()
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def _jaccard(a: list[str], b: list[str]) -> float:
    tg_a = _word_trigrams(a)
    tg_b = _word_trigrams(b)
    if not tg_a and not tg_b:
        return 0.0
    union = len(tg_a | tg_b)
    if union == 0:
        return 0.0
    return len(tg_a & tg_b) / union


def _extract_hook(candidate: dict) -> Optional[str]:
    overlay = candidate.get("text_overlay")
    if isinstance(overlay, dict):
        return _normalize(overlay.get("text", ""))
    if isinstance(overlay, str):
        return _normalize(overlay)
    return None


def _opening_words(prompt: str, n: int = 5) -> str:
    return " ".join(_normalize(prompt).split()[:n])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SIMILARITY_THRESHOLD = 0.60


def validate_prompt_distinctness(ranked_candidates: list[dict]) -> dict:
    """
    Validate that three image_prompts are sufficiently distinct for A/B/C testing.

    Args:
        ranked_candidates: List of candidate dicts from main_episode.ranked_candidates.
                           Expects exactly 3 entries each with "image_prompt" and
                           optionally "text_overlay".

    Returns:
        dict with keys:
            passed   — bool: True iff no failures
            failures — list of failure strings
            warnings — list of advisory strings

    Does not raise; callers decide whether to treat failures as blocking.
    Use validate_prompt_distinctness_or_raise() to raise PromptDistinctnessError.
    """
    failures: list[str] = []
    warnings: list[str] = []

    if len(ranked_candidates) != 3:
        warnings.append(
            f"distinctness check expects exactly 3 candidates, got {len(ranked_candidates)}"
        )
        return {"passed": True, "failures": failures, "warnings": warnings}

    prompts = [c.get("image_prompt", "") for c in ranked_candidates]
    labels  = [f"rank-{c.get('rank', i+1)}" for i, c in enumerate(ranked_candidates)]

    # ── Check 1: pairwise first-40-word Jaccard similarity ────────────────
    pairs = [(0, 1), (0, 2), (1, 2)]
    for i, j in pairs:
        w_i = _first_n_words(prompts[i])
        w_j = _first_n_words(prompts[j])
        sim = _jaccard(w_i, w_j)
        if sim > _SIMILARITY_THRESHOLD:
            failures.append(
                f"{labels[i]} and {labels[j]} image_prompts are too similar "
                f"(first-40-word Jaccard = {sim:.2f}, threshold = {_SIMILARITY_THRESHOLD}) — "
                "they likely share a common prefix; each prompt must open with a "
                "distinct visual idea"
            )

    # ── Check 2: all three hook texts identical ────────────────────────────
    hooks = [_extract_hook(c) for c in ranked_candidates]
    non_null = [h for h in hooks if h is not None]
    if len(non_null) == 3 and len(set(non_null)) == 1:
        failures.append(
            f"all three hook texts are identical: {non_null[0]!r} — "
            "each candidate must carry a distinct on-screen hook for A/B/C testing"
        )
    elif len(non_null) == 3 and len(set(non_null)) == 2:
        warnings.append(
            "two of three hook texts are identical — consider distinct hooks for each candidate"
        )

    # ── Check 3: opening collapse (all three start with same 5 words) ─────
    openings = [_opening_words(p) for p in prompts]
    if len(set(openings)) == 1 and openings[0]:
        failures.append(
            f"all three image_prompts share the same opening: {openings[0]!r} — "
            "this indicates primary_subject_identity was used as a common prefix; "
            "each prompt must open with a different visual starting point"
        )

    return {
        "passed": len(failures) == 0,
        "failures": failures,
        "warnings": warnings,
    }


def validate_prompt_distinctness_or_raise(ranked_candidates: list[dict]) -> dict:
    """
    Same as validate_prompt_distinctness() but raises PromptDistinctnessError on failure.
    """
    result = validate_prompt_distinctness(ranked_candidates)
    if not result["passed"]:
        raise PromptDistinctnessError(result["failures"])
    return result
