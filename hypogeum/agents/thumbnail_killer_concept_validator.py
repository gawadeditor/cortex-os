"""
hypogeum/agents/thumbnail_killer_concept_validator.py — Concept quality gate

Pre-generation deterministic validator for CONCEPT_DRIVEN_MODE.
Catches the specific failure modes that produce documentary illustration
instead of click psychology.

Root causes being blocked:
  1. Prompt opens with an object noun → still a scene description, not a visual argument
  2. More than 2 main visual elements in first sentence → object collage (ep_003 failure mode)
  3. Repeated cliché elements across all 3 prompts → A/B/C collapse to same image
  4. Wordmark described as the main visual subject → brand confusion
  5. Hook text explanatory rather than curiosity-driven → fails click psychology test

Checks are deterministic and fast (no API calls).

Public API:
    validate_concept_quality(ranked_candidates) -> dict
        Returns {"passed": bool, "failures": list[str], "warnings": list[str]}

    ConceptQualityError — raised by validate_concept_quality_or_raise()
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class ConceptQualityError(ValueError):
    """Raised when concept-driven prompts fail the quality gate."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__(
            f"Concept quality validation failed ({len(failures)} issue(s)): "
            + "; ".join(failures)
        )


# ---------------------------------------------------------------------------
# Vocabulary sets
# ---------------------------------------------------------------------------

# Physical object nouns that signal documentary illustration, not visual argument
_OBJECT_NOUNS: frozenset[str] = frozenset({
    "corroded", "bronze", "fragment", "fragments", "gear", "gears",
    "mechanism", "mechanisms", "dial", "dials", "tablet", "tablets",
    "ruin", "ruins", "column", "columns", "torch", "torches",
    "artifact", "artefact", "artifacts", "artefacts", "inscription",
    "inscriptions", "parchment", "scroll", "scrolls", "urn", "urns",
    "sarcophagus", "mummy", "statue", "statues", "relief", "hieroglyph",
    "hieroglyphs", "pillar", "pillars", "obelisk", "cartouche",
    "koine", "greek", "roman", "egyptian", "babylonian", "hellenistic",
    "ancient", "corrode",
})

# Cliché words that collapse A/B/C into the same object
_CLICHE_WORDS: frozenset[str] = frozenset({
    "gear", "gears", "mechanism", "mechanisms", "fragment", "fragments",
    "bronze", "corroded", "verdigris", "patina", "ruin", "ruins",
    "tablet", "tablets", "torch", "torches", "column", "columns",
    "dial", "dials", "inscription", "inscriptions", "koine",
    "antikythera",
})

# Words/patterns suggesting hook text is explanatory rather than curiosity-driven
_EXPLANATORY_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bknew how\b", re.IGNORECASE),
    re.compile(r"\bbuilt the\b", re.IGNORECASE),
    re.compile(r"\bwas made\b", re.IGNORECASE),
    re.compile(r"\bwas built\b", re.IGNORECASE),
    re.compile(r"\bthey did\b", re.IGNORECASE),
    re.compile(r"\bwe found\b", re.IGNORECASE),
    re.compile(r"\bhas been\b", re.IGNORECASE),
    re.compile(r"\bwas discovered\b", re.IGNORECASE),
    re.compile(r"\bremoved the\b", re.IGNORECASE),  # e.g. "SOMEONE REMOVED THE TEACHERS" is too explanatory
]

# Words that indicate curiosity gap (at least one required)
_CURIOSITY_MARKERS: frozenset[str] = frozenset({
    "shouldn't", "can't", "couldn't", "didn't", "doesn't",
    "vanished", "gone", "missing", "disappeared", "unknown",
    "impossible", "no one", "nobody", "never", "where", "who",
    "why", "what", "how", "when", "mystery", "secret",
    "shouldn", "cannot", "exist", "wrong",
})

# Wordmark phrase that should not appear as prompt subject
_WORDMARK_PHRASES: tuple[str, ...] = (
    "the hypogeum", "hypogeum chronicles", "the wordmark", "wordmark",
)

# Connectives between objects in an inventory list (suggests collage)
_INVENTORY_CONNECTIVES: list[re.Pattern] = [
    re.compile(r"\w+,\s*\w+,\s*\w+", re.IGNORECASE),  # "a, b, c" pattern
    re.compile(r"\bbeside (a|an|the)\b", re.IGNORECASE),
    re.compile(r"\bnext to (a|an|the)\b", re.IGNORECASE),
    re.compile(r"\bwith (a|an|the)\b", re.IGNORECASE),
    re.compile(r"\bresting on\b", re.IGNORECASE),
    re.compile(r"\bplaced on\b", re.IGNORECASE),
    re.compile(r"\bsitting on\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _first_word(prompt: str) -> str:
    """Return the first non-article, non-preposition word (lowercased)."""
    skip = {"a", "an", "the", "in", "on", "at", "with", "of", "into", "deep"}
    words = prompt.lower().split()
    for w in words[:5]:
        clean = re.sub(r"[^a-z]", "", w)
        if clean and clean not in skip:
            return clean
    return words[0].lower() if words else ""


def _first_sentence(prompt: str) -> str:
    """Return the first sentence (up to first period or 30 words, whichever shorter)."""
    parts = re.split(r"[.!?;—–]", prompt)
    first = parts[0] if parts else prompt
    words = first.split()
    return " ".join(words[:30])


def _count_concrete_nouns(text: str) -> int:
    """Count inventory/object nouns in the first sentence of the text."""
    sentence = _first_sentence(text).lower()
    count = 0
    for word in _CLICHE_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", sentence):
            count += 1
    return count


def _has_inventory_structure(text: str) -> bool:
    """Return True if the first sentence reads like an object inventory list.

    Requires BOTH a comma-heavy sentence AND the presence of cliché object nouns —
    commas alone are not enough (abstract phrases like "no maker, no school, no
    predecessor" are legitimate and should not be flagged as collage).
    """
    first = _first_sentence(text)
    for pattern in _INVENTORY_CONNECTIVES:
        if pattern.search(first):
            return True
    # Comma-heavy + cliché nouns together = object collage inventory
    if first.count(",") >= 3 and _count_concrete_nouns(first) >= 2:
        return True
    return False


def _is_explanatory_hook(hook: str) -> bool:
    """Return True if the hook text explains a fact rather than creating a gap."""
    hook_lower = hook.lower()
    for pat in _EXPLANATORY_PATTERNS:
        if pat.search(hook_lower):
            return True
    return False


def _has_curiosity_marker(hook: str) -> bool:
    """Return True if the hook text contains at least one curiosity-gap word."""
    hook_lower = hook.lower()
    for marker in _CURIOSITY_MARKERS:
        if marker in hook_lower:
            return True
    return False


def _extract_hook(candidate: dict) -> Optional[str]:
    overlay = candidate.get("text_overlay")
    if isinstance(overlay, dict):
        return overlay.get("text", "")
    if isinstance(overlay, str):
        return overlay
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_concept_quality(ranked_candidates: list[dict]) -> dict:
    """
    Run the concept quality gate on three image_prompts.

    Args:
        ranked_candidates: List from main_episode.ranked_candidates.

    Returns:
        {"passed": bool, "failures": list[str], "warnings": list[str]}
    """
    failures: list[str] = []
    warnings: list[str] = []

    if len(ranked_candidates) != 3:
        warnings.append(
            f"concept quality gate expects exactly 3 candidates, got {len(ranked_candidates)}"
        )
        return {"passed": True, "failures": failures, "warnings": warnings}

    prompts = [c.get("image_prompt", "") for c in ranked_candidates]
    labels  = [f"rank-{c.get('rank', i+1)}" for i, c in enumerate(ranked_candidates)]

    # ── Check 1: Prompt must not start with an object noun ─────────────────
    for label, prompt in zip(labels, prompts):
        fw = _first_word(prompt)
        if fw in _OBJECT_NOUNS:
            failures.append(
                f"{label} prompt starts with object noun {fw!r} — this is a documentary "
                "illustration opening, not a visual argument. Start with emotional atmosphere, "
                "void, or conceptual frame instead."
            )

    # ── Check 2: At most 2 main visual elements in the first sentence ───────
    for label, prompt in zip(labels, prompts):
        n = _count_concrete_nouns(prompt)
        if n > 2:
            failures.append(
                f"{label} prompt contains {n} cliché object-nouns in the first sentence "
                "— this is an object collage. A concept_driven_mode prompt must have "
                "at most 1 primary visual element with a single focused metaphor."
            )
        elif n == 2:
            warnings.append(
                f"{label} prompt contains 2 object-nouns in the opening — borderline collage. "
                "Consider reducing to 1 primary visual element."
            )

    # ── Check 3: Cliché words must not repeat across ALL THREE prompts ───────
    for word in _CLICHE_WORDS:
        pattern = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
        in_prompts = [bool(pattern.search(p)) for p in prompts]
        if all(in_prompts):
            failures.append(
                f"cliché word {word!r} appears in all three prompts — the three "
                "candidates will produce near-identical images centred on the same "
                "object. Remove it from at least two prompts."
            )

    # ── Check 4: Wordmark must not be described as the main visual ──────────
    for label, prompt in zip(labels, prompts):
        prompt_lower = prompt.lower()
        for phrase in _WORDMARK_PHRASES:
            if prompt_lower.startswith(phrase):
                failures.append(
                    f"{label} prompt starts with wordmark reference {phrase!r} — "
                    "the wordmark is channel branding, not the story subject."
                )

    # ── Check 5: Hook text must be curiosity-driven, not explanatory ─────────
    for label, candidate in zip(labels, ranked_candidates):
        hook = _extract_hook(candidate)
        if hook is None:
            continue
        if _is_explanatory_hook(hook):
            warnings.append(
                f"{label} hook text {hook!r} may be explanatory rather than "
                "curiosity-driven — it describes what happened rather than "
                "creating a gap the viewer must resolve by clicking."
            )
        if not _has_curiosity_marker(hook):
            warnings.append(
                f"{label} hook text {hook!r} contains no uncertainty or curiosity "
                "markers — consider words that imply impossibility, absence, or "
                "unresolved question (e.g. SHOULDN'T, VANISHED, WHERE, WHO, EXIST)."
            )

    # ── Check 6: Inventory structure in opening sentence ────────────────────
    for label, prompt in zip(labels, prompts):
        if _has_inventory_structure(prompt):
            failures.append(
                f"{label} prompt has inventory structure in the opening sentence "
                "(object beside/next to/with object, or 3+ commas) — this produces "
                "an object collage. Each prompt must describe one visual metaphor, "
                "not a staged arrangement of props."
            )

    return {
        "passed": len(failures) == 0,
        "failures": failures,
        "warnings": warnings,
    }


def validate_concept_quality_or_raise(ranked_candidates: list[dict]) -> dict:
    """Same as validate_concept_quality() but raises ConceptQualityError on failure."""
    result = validate_concept_quality(ranked_candidates)
    if not result["passed"]:
        raise ConceptQualityError(result["failures"])
    return result
