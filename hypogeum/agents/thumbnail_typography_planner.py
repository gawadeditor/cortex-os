"""
hypogeum/agents/thumbnail_typography_planner.py — Deterministic typography planner

Converts suggested_hook_text + emotional_trigger + composition_plan → typography_plan.

Split rules:
  - Hook ≤4 words: last 1 word → emphasis, earlier words → headline
  - Hook >4 words: last 2 words → emphasis, earlier words → headline

Sizing target: emphasis word character height ≈ 9–12% of frame height at 720p.
The compositor uses emphasis_scale to multiply the headline base_size (7% of height).
A single-word emphasis at scale 1.7 → ~12% character height. Two-word at 1.4 → ~10%.

Alignment:
  - Default: left
  - Center only when trigger is impossibility/awe/scale AND text_zone is "center"
"""
from __future__ import annotations

_CENTER_ELIGIBLE: set[str] = {"impossibility", "awe", "scale"}

# Emphasis scale multiplies the headline base size (compositor sets base = 7% of img_h).
# These values produce character heights of roughly 10–13% of frame height,
# which is the minimum for readability at 320px display width.
_EMPHASIS_SCALE_ONE_WORD:  float = 1.7   # single word: ~12% character height
_EMPHASIS_SCALE_TWO_WORDS: float = 1.4   # two words:  ~10% character height


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
    return _EMPHASIS_SCALE_ONE_WORD if len(emphasis.split()) == 1 else _EMPHASIS_SCALE_TWO_WORDS


def plan_typography(
    suggested_hook_text: str,
    emotional_trigger: str,
    composition_plan: dict,
) -> dict:
    """
    Convert suggested_hook_text + emotional_trigger + composition_plan
    into a typography_plan dict.

    Returns:
        headline        — setup words (earlier hook text)
        emphasis        — payload word(s) (last 1–2 words), rendered dominant
        layout          — "stacked"
        alignment       — "left" or "center"
        zone            — mirrors composition_plan.text_zone
        emphasis_scale  — multiplier applied to headline base size by compositor
        headline_weight — "regular" (lighter than emphasis but not invisible)
        emphasis_weight — "bold" (heavy, must stop the eye)
        emphasis_stroke — True: compositor adds thin stroke for legibility on
                          any background (warm cream outline, 1–2px)
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
        "headline_weight": "regular",
        "emphasis_weight": "bold",
        "emphasis_stroke": True,
    }
