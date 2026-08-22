"""
hypogeum/agents/shorts_metadata.py — Generates real, grounded YouTube
Shorts publication titles from each Short's own completed script content.

Fixes the historical placeholder-title defect: prior to this module,
hypogeum.agents.assembly_agent wrote `{"short_id": sid, "title": sid}` for
every Short (the short_id repeated as its own "title") -- confirmed as the
actual shape in every episode in production history that ever populated
metadata_source.shorts_titles. This module replaces that placeholder with
a real title derived only from that Short's own already-generated content.

Channel-agnostic (Factory Core): the prompt template and default
validation bounds here are generic and platform-derived (YouTube's own
title limit is 100 chars); any channel-specific editorial/SEO rules
(preferred length, tone, forbidden patterns) are supplied by the caller as
`editorial_rules` data, read by the caller from the channel's own Channel
Package -- never hardcoded here, and no Hypogeum-specific value appears
in this file.

Grounding against fabrication: the ONLY input given to the model for each
Short is that Short's own narration_beats and text_overlays -- nothing
else (no episode-wide research, no other Shorts' content) -- so a title
cannot reference a fact, name, date, or claim that Short does not already
state itself.

Public API:
    DEFAULT_EDITORIAL_RULES
    ShortsMetadataError
    generate_shorts_titles(shorts_scripts, *, config, episode_id,
        channel_id=None, editorial_rules=None, llm_call=None) -> list[dict]
"""

from __future__ import annotations

import json
from typing import Callable, List, Optional

from hypogeum.config import Config
from hypogeum.logger import get_logger

_AGENT_NAME = "shorts_metadata"

# Generic, platform-derived defaults (YouTube's technical title limit is
# 100 chars; these are conservative editorial defaults, not any specific
# channel's style) -- a Channel Package may override any of these via
# `editorial_rules`.
DEFAULT_EDITORIAL_RULES = {
    "min_length": 10,
    "preferred_max_length": 65,
    "max_length": 80,
    "tone": "accurate, curiosity-driven, no fabrication, no clickbait exaggeration",
    "forbidden_patterns": ["Part 1", "Part 2", "Part 3", "#"],
}


class ShortsMetadataError(RuntimeError):
    """
    Raised when a valid, grounded title cannot be produced for every
    requested Short. Never caught here to silently fall back to a
    placeholder or a fabricated value -- the caller (assembly_agent)
    decides how to degrade (see its own docstring), but this module
    itself never invents one.
    """


_SYSTEM_PROMPT = """You are an editorial assistant writing YouTube Shorts titles.

You will receive a JSON object with a "shorts" list. Each entry has a short_id
and its own narration_beats (the exact spoken script) and text_overlays (the
exact on-screen text already used in that Short). These are the ONLY facts you
may use -- do not introduce any fact, name, date, or claim that is not already
present in that Short's own narration_beats or text_overlays.

For EACH Short, write exactly ONE title following these rules:
{rules}

Titles must be distinct from each other and from any other Short's title.
Never output a title equal to its own short_id.

Return ONLY a JSON object of this exact shape, one entry per input Short, using
each Short's own short_id unchanged:
{{"titles": [{{"short_id": "...", "title": "..."}}, ...]}}"""


def _default_llm_call(
    system_prompt: str,
    input_package: dict,
    *,
    config: Config,
    episode_id: str,
    channel_id: Optional[str],
) -> dict:
    from hypogeum.openrouter import call_claude  # noqa: PLC0415

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(input_package, ensure_ascii=False)},
    ]
    result = call_claude(
        model_slug=getattr(config, "SHORTS_METADATA_MODEL_SLUG", "anthropic/claude-sonnet-4.6"),
        messages=messages, max_tokens=1024, config=config,
        episode_id=episode_id, agent=_AGENT_NAME, channel_id=channel_id,
    )
    raw = result.content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def generate_shorts_titles(
    shorts_scripts: List[dict],
    *,
    config: Config,
    episode_id: str,
    channel_id: Optional[str] = None,
    editorial_rules: Optional[dict] = None,
    llm_call: Optional[Callable[..., dict]] = None,
) -> List[dict]:
    """
    Returns [{"short_id": ..., "title": ...}, ...] -- exactly one entry
    per input Short, covering every short_id in `shorts_scripts` (order
    not guaranteed to match the input, but every input short_id is
    guaranteed present when this function returns without raising).

    `llm_call` is injectable so tests can supply a fake model fixture
    (never a live/paid call) — defaults to a real call through
    hypogeum.openrouter.call_claude() via _default_llm_call(). Signature:
    llm_call(system_prompt, input_package, *, config, episode_id,
    channel_id) -> dict (the parsed JSON response).

    Raises ShortsMetadataError (never falls back to a placeholder or a
    fabricated value) when: shorts_scripts is empty or has a duplicate/
    missing short_id, the model call raises, the response is not valid
    JSON or lacks a 'titles' list, the response is missing an entry for
    any requested short_id, a returned title is empty, a returned title
    equals its own short_id (the historical placeholder pattern), or a
    returned title violates editorial_rules' length bounds.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)
    if not shorts_scripts:
        raise ShortsMetadataError("no shorts_scripts supplied -- nothing to title")

    rules = {**DEFAULT_EDITORIAL_RULES, **(editorial_rules or {})}

    input_shorts = [
        {
            "short_id": s.get("short_id", ""),
            "narration_beats": [
                (b.get("text") or b.get("narration_text") or "") for b in s.get("narration_beats", [])
            ],
            "text_overlays": [ov.get("text", "") for ov in s.get("text_overlays", [])],
        }
        for s in shorts_scripts
    ]
    requested_ids = [s["short_id"] for s in input_shorts]
    if any(not sid for sid in requested_ids):
        raise ShortsMetadataError("shorts_scripts contains an entry with a missing/empty short_id")
    if len(set(requested_ids)) != len(requested_ids):
        raise ShortsMetadataError(f"shorts_scripts contains duplicate short_id(s): {requested_ids}")

    call = llm_call or _default_llm_call
    try:
        response = call(
            _SYSTEM_PROMPT.format(rules=json.dumps(rules)),
            {"shorts": input_shorts},
            config=config, episode_id=episode_id, channel_id=channel_id,
        )
    except Exception as exc:
        log.error("shorts_metadata_generation_failed", error=str(exc))
        raise ShortsMetadataError(f"shorts title generation failed: {exc}") from exc

    titles = response.get("titles") if isinstance(response, dict) else None
    if not isinstance(titles, list):
        raise ShortsMetadataError("model response did not contain a 'titles' list")

    result_by_id: dict = {}
    for entry in titles:
        if not isinstance(entry, dict):
            continue
        sid = (entry.get("short_id") or "").strip()
        title = (entry.get("title") or "").strip()
        if sid:
            result_by_id[sid] = title

    missing = [sid for sid in requested_ids if sid not in result_by_id]
    if missing:
        raise ShortsMetadataError(f"model response missing title(s) for short_id(s): {missing}")

    min_len = rules.get("min_length", 0)
    max_len = rules.get("max_length", 10_000)
    for sid in requested_ids:
        title = result_by_id[sid]
        if not title:
            raise ShortsMetadataError(f"model returned an empty title for short_id={sid!r}")
        if title.strip().lower() == sid.strip().lower():
            raise ShortsMetadataError(f"model returned a placeholder title (equal to short_id) for short_id={sid!r}")
        if not (min_len <= len(title) <= max_len):
            raise ShortsMetadataError(
                f"model returned a title of length {len(title)} for short_id={sid!r}, "
                f"outside the allowed [{min_len}, {max_len}] range"
            )

    distinct_titles = {result_by_id[sid].strip().lower() for sid in requested_ids}
    if len(distinct_titles) != len(requested_ids):
        raise ShortsMetadataError("model returned two or more identical titles across distinct Shorts")

    return [{"short_id": sid, "title": result_by_id[sid]} for sid in requested_ids]
