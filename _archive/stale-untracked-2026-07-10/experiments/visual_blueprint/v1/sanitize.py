"""
Blueprint Engine v1 — sanitize.py
Post-compose wording cleanup applied by both composers.

Rules:
  1. Deduplicate echoed participial adjective: "fracturing across the fractured" → "fracturing across the"
  2. "surfaces from absolute darkness" → "emerges from absolute darkness"
  3. Restore proper-noun capitalisation lost by .lower(): aegean → Aegean
  4. Capitalise first letter after ". " in prose (sentence boundary after period)
"""

import re


def _lower_first(s: str) -> str:
    """Lowercase only the first character, preserving interior proper nouns."""
    s = s.rstrip(".")
    return s[0].lower() + s[1:] if s else s


def sanitize(text: str) -> str:
    # 1. Remove participial adjective that echoes its governing verb
    text = re.sub(r"\bfracturing across the fractured\b", "fracturing across the", text)

    # 2. Non-human subjects surfacing → emerging
    text = text.replace("surfaces from absolute darkness", "emerges from absolute darkness")

    # 3. Restore proper-noun capitalisation
    text = re.sub(r"\baegean\b", "Aegean", text)

    # 4. Capitalise after sentence boundary (". " followed by a lowercase letter)
    text = re.sub(r"(?<=\. )([a-z])", lambda m: m.group(1).upper(), text)

    return text
