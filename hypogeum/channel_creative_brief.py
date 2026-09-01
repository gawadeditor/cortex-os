"""
hypogeum/channel_creative_brief.py — the editorial identity of one channel.

The North Star: "Cortex Core contains ZERO channel knowledge." The content
agents (Executive Producer, Research Director, Narrative Architect, Script
Engine, editorial QA) used to have "history and ancient mystery content"
baked into their system prompts — a large leak of one channel's identity
into the shared Core.

This module produces the single structured brief each of those agents reads
from its input, so the Core prompts describe a *role* and the channel
supplies the *subject, audience, format, and standard of quality*. Built
deterministically from the channel's BrandProfile + ChannelProfile +
operations profile.

Public API:
    build_creative_brief(channel_id, *, db_path=None) -> dict
    channel_brief_for(channel_id) -> dict   # convenience for agents; tolerates None
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger()

# A neutral brief for a channel with no profile — keeps the pipeline running
# rather than hard-failing, but names the gap.
_FALLBACK = {
    "channel_id": "",
    "channel_name": "",
    "genre": "short-form informative video",
    "format": "a short narrated video",
    "audience": "a general audience",
    "tone": "clear and engaging",
    "language": "en",
    "content_pillars": [],
    "what_makes_a_good_topic": (
        "A good topic is genuinely interesting to this channel's audience, has "
        "clear visual potential for AI-generated imagery, and offers a fresh "
        "angle rather than a generic overview."
    ),
    "topic_taxonomy": [],
    "voice_notes": "",
    "shorts_native": False,
    "_source": "fallback (channel has no brand profile)",
}


def _gentle(tone: str) -> bool:
    t = tone.lower()
    return any(w in t for w in ("gentle", "whimsical", "soothing", "calm", "cozy",
                                "cosy", "playful", "warm", "wonder", "dreamy", "tender"))


def _investigative(tone: str) -> bool:
    t = tone.lower()
    return any(w in t for w in ("investigat", "evidence", "mystery", "forensic",
                                "analytical", "rigorous", "documentary"))


def _pillar_head(p: str) -> str:
    for sep in (" — ", " - ", ":", ";"):
        if sep in p:
            return p.split(sep)[0].strip()
    return p.strip()


def _standard_of_quality(genre: str, audience: str, tone: str, pillars: list) -> str:
    """One paragraph: what makes a GOOD topic for THIS channel."""
    if _gentle(tone):
        return (
            f"A good topic for this channel is soothing and age-appropriate for "
            f"{audience or 'young children'}: it carries a small sense of wonder, "
            f"resolves gently and reassuringly, has no scary or over-stimulating "
            f"content, works as a calm wind-down, and fits one of the channel's "
            f"story pillars ({', '.join(_pillar_head(p) for p in pillars) or 'gentle everyday moments'}). "
            f"Prefer warmth, simplicity, and a satisfying quiet ending over "
            f"novelty, competitiveness, or 'unresolved mystery'."
        )
    if _investigative(tone):
        return (
            f"A good topic for this channel has genuine unresolved questions that "
            f"deepen with research, strong visual potential for reconstructions and "
            f"artefacts, a differentiated angle not already saturated elsewhere, and "
            f"real audience demand among {audience or 'this channel’s audience'}. "
            f"Every dimension contributes; a topic strong on demand but weak on a "
            f"fresh angle is not automatically approved."
        )
    return (
        f"A good topic for this channel genuinely interests {audience or 'the audience'}, "
        f"fits the channel's {genre} focus and its pillars "
        f"({', '.join(_pillar_head(p) for p in pillars) or 'core themes'}), has clear "
        f"visual potential, and offers a specific fresh angle rather than a generic "
        f"overview."
    )


def _taxonomy(genre: str, pillars: list, tone: str) -> list:
    """The topic_category vocabulary for this channel (replaces the old
    hardcoded history-only enum)."""
    heads = [_pillar_head(p).lower().replace(" ", "_")[:40] for p in pillars if p]
    heads = [h for h in heads if h]
    if heads:
        return heads
    if _gentle(tone):
        return ["sleepy_adventure", "dream_journey", "gentle_lesson", "lullaby_moment"]
    if _investigative(tone):
        return ["ancient_mystery", "historical_event", "lost_civilisation",
                "scientific_enigma", "archaeological_discovery", "natural_phenomenon"]
    return ["explainer", "story", "profile", "phenomenon"]


def build_creative_brief(channel_id: str, *, db_path: Optional[Path] = None) -> dict:
    """The editorial brief for `channel_id`. Never raises — returns the
    fallback brief if the channel has no profile."""
    try:
        from hypogeum.channel_config import (
            get_brand_profile, get_channel_operations_profile, get_channel_profile,
        )
        ch = get_channel_profile(channel_id, db_path=db_path)
        if not ch:
            return {**_FALLBACK, "channel_id": channel_id}
        bp = get_brand_profile(ch.brand_profile_id, db_path=db_path) if ch.brand_profile_id else None
        if not bp:
            return {**_FALLBACK, "channel_id": channel_id, "channel_name": ch.name}

        desc = (ch.description or "").strip()
        # "Niche / genre: X" is a common convention in the description field.
        genre = ""
        for line in desc.splitlines():
            low = line.lower()
            if "genre" in low or "niche" in low:
                genre = line.split(":", 1)[-1].strip()
                break
        genre = genre or (desc.split(".")[0].strip() if desc else "") or bp.name

        tone = (bp.tone or "").strip()
        audience = (bp.target_audience or "").strip()
        pillars = list(bp.content_pillars or [])

        short_native = False
        try:
            ops = get_channel_operations_profile(channel_id, db_path=db_path)
            cad = getattr(ops, "production_cadence", None) or {}
            short_native = int(cad.get("shorts_per_week") or 0) > 0 and \
                int(cad.get("videos_per_week") or 0) == 0
        except Exception:  # noqa: BLE001
            pass

        fmt = "a one-minute vertical animated short" if short_native else "a narrated video episode"

        return {
            "channel_id": channel_id,
            "channel_name": ch.name,
            "genre": genre,
            "format": fmt,
            "audience": audience or "this channel's audience",
            "tone": tone or "clear and engaging",
            "language": bp.language or "en",
            "content_pillars": pillars,
            "what_makes_a_good_topic": _standard_of_quality(genre, audience, tone, pillars),
            "topic_taxonomy": _taxonomy(genre, pillars, tone),
            "voice_notes": (bp.voice_guidelines or "").strip()[:600],
            "shorts_native": short_native,
            "_source": "brand_profile",
        }
    except Exception as exc:  # noqa: BLE001
        log.error("channel_creative_brief_error", channel_id=channel_id, error=str(exc))
        return {**_FALLBACK, "channel_id": channel_id}


def channel_brief_for(channel_id: Optional[str]) -> dict:
    """Convenience for agents: the brief for `channel_id`, or the fallback
    brief when channel_id is falsy. Never raises."""
    return build_creative_brief(channel_id or "")
