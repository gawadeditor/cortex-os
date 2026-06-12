"""
hypogeum/humanizer.py — Deterministic text humanizer (v2.7.0 patterns)

Applies a fixed set of regex-based transformations to reduce AI writing patterns.
No LLM calls. No network calls. Safe to call on any plain string.

Supported field names (informational — all fields receive the same treatment):
  "title"        — episode or short title
  "description"  — YouTube / Facebook description body
  "ai_disclosure" — AI disclosure text
  "narration"    — beat narration prose
  "cta"          — call-to-action beat text
  "logline"      — episode logline

Patterns applied (subset of humanizer v2.7.0):
  P01 — Remove significance-inflation phrases
  P07 — Replace overused AI vocabulary words
  P14 — Replace em dashes and en dashes (HARD CONSTRAINT)
  P20 — Strip chatbot discourse markers at sentence start
  P23 — Simplify filler phrases
  P24 — Reduce excessive hedging chains
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

# P14 — Em dash and en dash replacements (applied first, hard constraint)
# Em dash (U+2014) surrounded by word chars → comma + space
# Em dash at other positions → space
# En dash (U+2013) → spaced hyphen
_DASH_PATTERNS: list[tuple[str, str]] = [
    (r"(\w)\u2014(\w)", r"\1, \2"),   # word—word → word, word
    (r"\s*\u2014\s*",   ", "),         # any other em dash → ", "
    (r"\s*\u2013\s*",   " - "),        # en dash → spaced hyphen
]

# P01 — Significance inflation (phrase → simpler phrase)
_INFLATION_PATTERNS: list[tuple[str, str]] = [
    (r"\bstands as a testament to\b",    "shows"),
    (r"\bis a testament to\b",           "shows"),
    (r"\bserves as a testament to\b",    "shows"),
    (r"\btestifies to\b",                "shows"),
    (r"\bpivotal moment\b",              "moment"),
    (r"\blandmark moment\b",             "moment"),
    (r"\bgroundbreaking discovery\b",    "discovery"),
    (r"\bmonumental achievement\b",      "achievement"),
]

# P07 — Overused AI vocabulary (word/phrase → plainer alternative)
_VOCABULARY_PATTERNS: list[tuple[str, str]] = [
    (r"\bdelve into\b",          "explore"),
    (r"\bdelve\b",               "explore"),
    (r"\bshowcase\b",            "show"),
    (r"\bmeticulous(?:ly)?\b",   "careful"),
    (r"\bseamlessly\b",          ""),
    (r"\bintricate\b",           "complex"),
    (r"\belaborate\b",           "detailed"),
    (r"\bleverage\b",            "use"),
    (r"\bunlock the secrets of\b", "explore"),
    (r"\bunlock the mystery of\b", "examine"),
]

# P20 — Chatbot discourse markers (remove phrase + trailing punctuation/space)
_DISCOURSE_MARKERS: list[str] = [
    r"Certainly[!,.]?\s*",
    r"Absolutely[!,.]?\s*",
    r"Of course[!,.]?\s*",
    r"Great question[!,.]?\s*",
    r"I hope this helps[!,.]?\s*",
    r"Feel free to (?:ask|let me know)[^.]*\.\s*",
    r"It['']s (?:worth noting|important to note) that\s*",
    r"It is (?:worth noting|important to note) that\s*",
    r"(?:It(?:'s)? )important to (?:note|highlight) that\s*",
]

# P23 — Filler phrases (phrase → concise replacement; compiled IGNORECASE)
_FILLER_PATTERNS: list[tuple[str, str]] = [
    (r"\bin order to\b",                "to"),
    (r"\bdue to the fact that\b",       "because"),
    (r"\bat this point in time\b",      "now"),
    (r"\bfor all intents and purposes\b", "essentially"),
    (r"\bin the event that\b",          "if"),
    (r"\bprior to\b",                   "before"),
    (r"\bsubsequent to\b",              "after"),
]

# P24 — Excessive hedging chains (normalize to single hedge)
_HEDGING_PATTERNS: list[tuple[str, str]] = [
    (r"\bcould potentially possibly\b",  "could"),
    (r"\bmight possibly perhaps\b",      "might"),
    (r"\bpotentially possibly\b",        "potentially"),
    (r"\bit could be argued that\b",     ""),
    (r"\bone might (?:suggest|argue) that\b", ""),
    (r"\bit (?:is|seems) (?:clear|evident|obvious) that\b", ""),
]


# ---------------------------------------------------------------------------
# Compiled patterns (module-level, built once)
# ---------------------------------------------------------------------------

def _compile(pairs: list[tuple[str, str]]) -> list[tuple[re.Pattern, str]]:
    return [(re.compile(p, re.IGNORECASE), r) for p, r in pairs]


def _compile_removals(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_COMPILED_DASH      = [(re.compile(p), r) for p, r in _DASH_PATTERNS]
_COMPILED_INFLATION = _compile(_INFLATION_PATTERNS)
_COMPILED_VOCAB     = _compile(_VOCABULARY_PATTERNS)
_COMPILED_DISCOURSE = _compile_removals(_DISCOURSE_MARKERS)
_COMPILED_FILLER    = _compile(_FILLER_PATTERNS)
_COMPILED_HEDGING   = _compile(_HEDGING_PATTERNS)

# Whitespace cleanup (applied last)
_RE_MULTI_SPACE  = re.compile(r"  +")
_RE_SPACE_COMMA  = re.compile(r"\s+,")
_RE_LEADING_COMMA = re.compile(r"^,\s*")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def humanize_field(text: str, field_name: str = "") -> str:
    """
    Apply deterministic humanizer patterns to a plain text string.

    All patterns are purely additive corrections — no content is invented and
    no structural information is removed. The function is idempotent: calling
    it twice produces the same result as calling it once.

    Args:
        text:       Plain text string to process. Must not be JSON or HTML.
        field_name: Informational — which field this text came from.
                    Currently all fields receive the same treatment.
                    Accepted values: "title", "description", "ai_disclosure",
                    "narration", "cta", "logline". Unrecognised values are safe.

    Returns:
        Humanized string. If text is empty or whitespace-only, returns it unchanged.
    """
    if not text or not text.strip():
        return text

    t = text

    # P14 — Dashes (HARD CONSTRAINT — must run first)
    for pattern, replacement in _COMPILED_DASH:
        t = pattern.sub(replacement, t)

    # P20 — Chatbot discourse markers
    for pattern in _COMPILED_DISCOURSE:
        t = pattern.sub("", t)

    # P01 — Significance inflation
    for pattern, replacement in _COMPILED_INFLATION:
        t = pattern.sub(replacement, t)

    # P07 — AI vocabulary
    for pattern, replacement in _COMPILED_VOCAB:
        t = pattern.sub(replacement, t)

    # P23 — Filler phrases
    for pattern, replacement in _COMPILED_FILLER:
        t = pattern.sub(replacement, t)

    # P24 — Excessive hedging
    for pattern, replacement in _COMPILED_HEDGING:
        t = pattern.sub(replacement, t)

    # Cleanup — collapse multiple spaces, fix space-before-comma
    t = _RE_MULTI_SPACE.sub(" ", t)
    t = _RE_SPACE_COMMA.sub(",", t)
    t = _RE_LEADING_COMMA.sub("", t)
    t = t.strip()

    return t


def humanize_beats(beats: list) -> list:
    """
    Apply humanize_field to the ``text`` field of every beat in a beats list.

    Modifies and returns the same list (in-place mutation — caller's reference
    remains valid). Non-dict entries and entries without a "text" key are skipped.

    Args:
        beats: List of beat dicts, each expected to have a "text" str key.

    Returns:
        The same list with beat["text"] values humanized.
    """
    for beat in beats:
        if isinstance(beat, dict) and isinstance(beat.get("text"), str):
            field = "cta" if beat.get("beat_type") == "cta" else "narration"
            beat["text"] = humanize_field(beat["text"], field)
    return beats
