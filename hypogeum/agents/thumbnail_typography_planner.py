"""
hypogeum/agents/thumbnail_typography_planner.py — Deterministic typography planner

Converts suggested_hook_text + emotional_trigger + composition_plan → typography_plan.

Split rules:
  - Hook ≤4 words: last 1 word → emphasis, earlier words → headline
  - Hook >4 words: last 2 words → emphasis, earlier words → headline

Alignment rules:
  - impossibility / awe / scale: center if composition_plan.text_zone == "center", else left
  - loss / betrayal / dread / discovery: always left
  - unknown trigger: left (safe default)

emphasis_scale range: 2.0–2.5
  - 2.5 when emphasis is 1 word
  - 2.0 when emphasis is 2 words
"""
from __future__ import annotations

_CENTER_ELIGIBLE: set[str] = {"impossibility", "awe", "scale"}

_MIN_EMPHASIS_SCALE: float = 2.0
_MAX_EMPHASIS_SCALE: float = 2.5


def _split_hook(text: str) -> tuple[str, str]:
    """
    Split hook text into (headline, emphasis).

    Hook ≤4 words: last 1 word is emphasis.
    Hook >4 words: last 2 words are emphasis.
    Single-word hook: headline is empty string, emphasis is the word.
    """
    words = text.strip().split()
    if not words:
        return ("", "")
    if len(words) == 1:
        return ("", words[0])
    if len(words) <= 4:
        return " ".join(words[:-1]), words[-1]
    return " ".join(words[:-2]), " ".join(words[-2:])


def _alignment(emotional_trigger: str, text_zone: str) -> str:
    if emotional_trigger in _CENTER_ELIGIBLE and text_zone == "center":
        return "center"
    return "left"


def _emphasis_scale(emphasis: str) -> float:
    return _MAX_EMPHASIS_SCALE if len(emphasis.split()) == 1 else _MIN_EMPHASIS_SCALE


def plan_typography(
    suggested_hook_text: str,
    emotional_trigger: str,
    composition_plan: dict,
) -> dict:
    """
    Convert suggested_hook_text + emotional_trigger + composition_plan
    into a typography_plan dict.

    Args:
        suggested_hook_text: Raw hook text (e.g., "IT SHOULDN'T EXIST").
        emotional_trigger:   Concept emotional trigger.
        composition_plan:    Pre-computed composition_plan for this concept.
                             Must contain a "text_zone" key.

    Returns:
        typography_plan dict with keys:
            headline        — earlier portion of hook text
            emphasis        — final 1–2 words (rendered larger/bolder)
            layout          — always "stacked"
            alignment       — "left" or "center"
            zone            — mirrors composition_plan.text_zone
            emphasis_scale  — 2.0 or 2.5
            headline_weight — "light"
            emphasis_weight — "bold"
    """
    text_zone  = composition_plan.get("text_zone", "lower_left")
    headline, emphasis = _split_hook(suggested_hook_text)
    return {
        "headline":        headline,
        "emphasis":        emphasis,
        "layout":          "stacked",
        "alignment":       _alignment(emotional_trigger, text_zone),
        "zone":            text_zone,
        "emphasis_scale":  _emphasis_scale(emphasis),
        "headline_weight": "light",
        "emphasis_weight": "bold",
    }
