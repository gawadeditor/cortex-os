"""
hypogeum/audience_profile.py — turn the measured audience demographics
(channel_audience, populated by zernio_native_sync) into one plain
sentence the content agents can act on.

The brand profile's `target_audience` is an aspiration set at channel
creation. This is who is *actually* watching — and for hypogeum that is a
very different person (predominantly 55+ and male on YouTube, the biggest
platform). `build_creative_brief()` surfaces this as `observed_audience`
alongside the configured `audience`, so every content agent sees it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# YouTube / Instagram age buckets, oldest-leaning ones grouped as "older".
_OLDER_AGE = {"45-54", "55-64", "65-", "65+", "65-100"}
_YOUNGER_AGE = {"13-17", "18-24", "25-34"}


def _dominant(rows: list) -> Optional[tuple]:
    """(label, pct) of the largest slice, or None."""
    best = None
    for r in rows or []:
        try:
            pct = float(r.get("pct"))
        except (TypeError, ValueError):
            continue
        if best is None or pct > best[1]:
            best = (str(r.get("label")), pct)
    return best


def _age_phrase(rows: list) -> Optional[str]:
    if not rows:
        return None
    older = sum(float(r["pct"]) for r in rows
               if r.get("label") in _OLDER_AGE and isinstance(r.get("pct"), (int, float)))
    younger = sum(float(r["pct"]) for r in rows
                 if r.get("label") in _YOUNGER_AGE and isinstance(r.get("pct"), (int, float)))
    dom = _dominant(rows)
    if older >= 55:
        return f"skews strongly older ({older:.0f}% aged 45+)"
    if older >= 40:
        return f"leans older ({older:.0f}% aged 45+)"
    if younger >= 55:
        return f"skews young ({younger:.0f}% aged 13–34)"
    if dom:
        return f"peaks at {dom[0]} ({dom[1]:.0f}%)"
    return None


def _gender_phrase(rows: list) -> Optional[str]:
    dom = _dominant(rows)
    if not dom or dom[1] < 60:
        return None
    return f"predominantly {dom[0]} ({dom[1]:.0f}%)"


def summarize_observed_audience(channel_id: str, *,
                                db_path: Optional[Path] = None) -> Optional[str]:
    """One sentence describing who actually watches, from the most recent
    demographics snapshot. None when there is no demographic data yet
    (a platform needs ~100+ followers before its API returns any)."""
    try:
        from hypogeum.publishing.zernio_native_sync import get_channel_audience
        aud = get_channel_audience(channel_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        return None
    if not aud:
        return None

    # YouTube is the reference platform (largest audience); fall back to IG.
    for platform in ("youtube", "instagram", "facebook", "tiktok"):
        block = aud.get(platform)
        if not block:
            continue
        parts = []
        ap = _age_phrase(block.get("age") or [])
        gp = _gender_phrase(block.get("gender") or [])
        if ap:
            parts.append(ap)
        if gp:
            parts.append(gp)
        geo = _dominant(block.get("country") or [])
        if geo and geo[1] >= 30:
            parts.append(f"mostly in {geo[0]} ({geo[1]:.0f}%)")
        if not parts:
            continue
        return (f"Measured {platform} audience {', '.join(parts)}. "
                f"Pitch references, vocabulary and pacing to this real "
                f"audience, not the channel's original target.")
    return None


_MAX_VOICE_COMMENTS = 12
_MAX_VOICE_CHARS = 1400


def summarize_audience_voice(channel_id: str, *,
                             db_path: Optional[Path] = None) -> Optional[str]:
    """Recent audience comments (their own words) as a compact block for the
    content agents — what viewers are actually reacting to and asking for.
    None when the channel has no non-owner comments yet."""
    try:
        from hypogeum.publishing.zernio_native_sync import get_recent_audience_comments
        rows = get_recent_audience_comments(channel_id, limit=_MAX_VOICE_COMMENTS,
                                            db_path=db_path)
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    lines = []
    used = 0
    for r in rows:
        t = " ".join((r.get("text") or "").split())
        if not t:
            continue
        likes = r.get("like_count") or 0
        entry = f"- ({r.get('platform')}{f', {likes}♥' if likes else ''}) {t[:220]}"
        if used + len(entry) > _MAX_VOICE_CHARS:
            break
        lines.append(entry)
        used += len(entry)
    if not lines:
        return None
    return ("Recent audience comments (their words — what actually landed, "
            "what they asked for, correct any recurring misconception in a "
            "future episode rather than the current one):\n" + "\n".join(lines))
