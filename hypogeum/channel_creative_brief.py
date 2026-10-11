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
    "observed_audience": None,
    "audience_voice": None,
    "tone": "clear and engaging",
    "language": "en",
    "content_pillars": [],
    "what_makes_a_good_topic": (
        "A good topic is genuinely interesting to this channel's audience, has "
        "clear visual potential for AI-generated imagery, and offers a fresh "
        "angle rather than a generic overview."
    ),
    "virality_directive": (
        "Every script is built to be watched to the end and to be worth sharing. "
        "Open on the most interesting specific thing in the story, keep one clear "
        "through-line, pay off what the opening promised, and end on a line that "
        "lands. Concrete and specific beats vague and comprehensive."
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


def _hook_library(kind: str) -> str:
    """The Hook Pattern Library text, appended to the virality directive so
    NA / SE / EP all inherit it (they already read virality_directive)."""
    try:
        from hypogeum.hook_patterns import render_hook_library
        return render_hook_library(kind)
    except Exception:  # noqa: BLE001 -- never break brief assembly over the hook menu
        return ""


def _virality_directive(tone: str, audience: str, short_native: bool) -> str:
    """What "built to be watched to the last second and worth sharing" means
    for THIS channel. Read by the Narrative Architect and Script Engine —
    every script has this as its job, expressed in the channel's own
    mechanics (an investigative channel earns the watch with tension; a
    bedtime channel earns it with warmth and the return habit)."""
    length = "one ~45-60s vertical short" if short_native else "one narrated episode"
    if _investigative(tone):
        return (
            f"Every script for this channel is engineered to stop the scroll and hold "
            f"attention to the final frame of {length}. A script that only informs has "
            f"failed. Mechanics:\n"
            f"- HOOK (first spoken line, first ~2 seconds): lead with the single most "
            f"provocative TRUE thing in the story — a claim that contradicts what the "
            f"viewer assumes, a number that sounds impossible, or a direct challenge to "
            f"the viewer. Never open with a date, a place name, or 'In [year]…'. The "
            f"hook may dramatize a real fact or a real unresolved question; it must NOT "
            f"invent, overstate past the evidence, or promise a payoff the research "
            f"cannot deliver.\n"
            f"- ONE SPINE: a single question or tension opened by the hook, escalated by "
            f"every beat, never dropped. A fact that does not feed the spine is cut.\n"
            f"- PAYOFF: the spine's question is answered — or explicitly named as a "
            f"genuine unknown — before the final beat. Never tease 'the real story' and "
            f"then withhold it.\n"
            f"- STINGER: the last spoken line is a reframe or a live implication that "
            f"makes the viewer want to argue in the comments — not a subscribe line.\n"
            f"- SHAREABLE CORE: one sentence a viewer would repeat to a friend; build "
            f"the script so that sentence is impossible to miss.\n"
            f"- VOICE: tense, vivid, concrete, present tense where it can be, second "
            f"person allowed. No hedging, no throat-clearing; every sentence earns the "
            f"next.\n"
            f"- SECOND PUNCH IMMEDIATELY AFTER THE HOOK: measured against what was actually "
            f"said and shown (2026-10, 11 videos, scripts matched to their YouTube retention "
            f"curves), the single biggest exit in 9 of 11 shorts lands 4-6.5 seconds in — "
            f"one to three seconds into beat 2, right after the hook line ends — and beat 2 "
            f"was always the explaining line (a reframe, a grounding, an age, a definition). "
            f"The hook earns the viewer; a calm explaining second line loses them. So beat 2 "
            f"must be a SECOND punch, not an explanation: escalate the same tension with a "
            f"new specific fact or a sharper turn, and put the explaining in beat 3 or later. "
            f"Never open beat 2 with a definition, a date or a recap.\n"
            f"- LOOP THE END: the strongest videos show replays (the opening segment is "
            f"watched more than once). Make the final line connect back to the opening "
            f"image or claim so a viewer who lets it roll starts again without noticing."
            + _hook_library("investigative")
        )
    if _gentle(tone):
        return (
            f"Every script for this channel is a COMPLETE LITTLE STORY with a "
            f"beginning, a middle and an end — a named character who wants something, "
            f"a small gentle problem in the way, and a satisfying resolution that "
            f"lets everyone (and the watching child) settle to sleep. A string of "
            f"pretty images with no problem and no change is NOT a story and fails "
            f"this channel — that is the single most common mistake, do not make it. "
            f"The story is what makes a child ask for another one tomorrow night; "
            f"warmth and a soft voice are how it is told, not a substitute for it. "
            f"Structure for {length}:\n"
            f"- OPEN (beats 1-2): name the character and the place, and give the "
            f"character ONE small, concrete want or feeling that is not yet met — "
            f"sleepy but the room feels too big, can't find the way back to bed, "
            f"misses saying goodnight to the moon, wants to help a smaller friend "
            f"settle. Warm and specific, never a question, never 'Once upon a time'.\n"
            f"- MIDDLE (beats 3-6): the character sets out and makes two or three "
            f"gentle attempts, usually meeting one or two soft friends along the way "
            f"who each help a little. There IS a small obstacle (the path is dark, "
            f"the moon is hiding, the friend is still awake) — gentle, never scary, "
            f"never urgent, but real enough that solving it means something.\n"
            f"- THE TURN (around beat 6-7): the one moment the problem eases — a "
            f"kindness, a little patience, a small brave step, or help from a friend. "
            f"This is the heart of the story; without it there is no story.\n"
            f"- CLOSE (beats 7-9): the want is met, the problem is fully gone, the "
            f"character (and any friends) settle down, and the last two beats visibly "
            f"wind the energy DOWN — motion stops, light dims, the frame goes still. "
            f"No loose thread a child could lie awake worrying about.\n"
            f"- A REFRAIN: one short line or sound the story returns to two or three "
            f"times ('and the night grew a little softer') — what a child asks to "
            f"hear again.\n"
            f"- RETURN LINE: a calm final line that names the character and invites "
            f"'another story about <name> tomorrow night' — the channel is building a "
            f"small cast of recurring characters a child gets attached to; that "
            f"attachment, plus the story, is the growth engine.\n"
            f"- VOICE: short sentences, simple words a {audience or 'child aged 2-6'} "
            f"already knows, soft sounds, a slow lullaby cadence with room to breathe "
            f"between lines. Present tense. Concrete over abstract every time."
            + _hook_library("gentle")
        )
    return (
        f"Every script for this channel is built to be watched to the end and to be "
        f"worth sharing. Open {length} on the most interesting specific thing in the "
        f"story — never a generic setup. Keep one clear through-line the whole way, "
        f"pay off what the opening promised, and end on a line that lands rather than "
        f"trailing off. Concrete and specific beats vague and comprehensive."
        + _hook_library("neutral")
    )


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

        observed_audience = None
        audience_voice = None
        try:
            from hypogeum.audience_profile import (
                summarize_observed_audience, summarize_audience_voice,
            )
            observed_audience = summarize_observed_audience(channel_id, db_path=db_path)
            audience_voice = summarize_audience_voice(channel_id, db_path=db_path)
        except Exception:  # noqa: BLE001
            pass

        return {
            "channel_id": channel_id,
            "channel_name": ch.name,
            "genre": genre,
            "format": fmt,
            "audience": audience or "this channel's audience",
            "observed_audience": observed_audience,
            "audience_voice": audience_voice,
            "tone": tone or "clear and engaging",
            "language": bp.language or "en",
            "content_pillars": pillars,
            "what_makes_a_good_topic": _standard_of_quality(genre, audience, tone, pillars),
            "virality_directive": _virality_directive(tone, audience, short_native),
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
