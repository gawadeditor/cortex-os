"""
hypogeum/brand/channel_identity.py — per-channel visual identity router.

The North Star: "The Channel contains identity." A cloned channel must
look like its own brand, not a re-skin of Hypogeum.

hypogeum.brand.visual_identity is the canonical Hypogeum identity, loaded
from docs/brand_guardian/brand_bible_hypogeum.json. This module routes each
channel to the right identity source:

  * A channel with a brand-bible file (docs/brand_guardian/brand_bible_<id>.json)
    -> hypogeum.brand.visual_identity  (today only Hypogeum has one; a future
    change makes that loader per-channel-bible-aware).
  * A channel with a BrandProfile carrying a visual_style
    -> a deterministic directive built from that profile
    (hypogeum.channel_config.BrandProfile: visual_style, tone, content_pillars,
    color_palette).
  * Anything else (no channel_id, no profile, no visual_style)
    -> hypogeum.brand.visual_identity  (the safe default — brand guidance is
    better than none).

The directives are DETERMINISTIC: the Production Plan compiler and the paid
submission gate both assemble the scene prompt through
hypogeum.video_prompt_writer.assemble_video_scene_prompt(); if the brand
text differed between them the plan's prompt fingerprint would not match.

Public API:
    build_channel_visual_identity_directive(channel_id, mode, db_path=None) -> str
    get_channel_adaptive_video_forbidden_items(channel_id, db_path=None) -> list[str]
    channel_uses_own_brand_profile(channel_id, db_path=None) -> bool
"""

from __future__ import annotations

import contextlib
import contextvars
from pathlib import Path
from typing import Optional

from hypogeum.brand import visual_identity

# The channel whose identity is active for the current pipeline run. Set once
# by hypogeum.pipeline.run_pipeline() via active_channel(); read by
# hypogeum.brand.visual_identity's directive builders so every generation
# surface (video scenes, thumbnails, entity references) inherits it without
# threading channel_id through each call site. A run compiles its Production
# Plan and submits its assets in the same process, so the plan's declared
# prompt fingerprints and the submission gate see the same active channel.
_ACTIVE_CHANNEL: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hypogeum_active_channel", default=None)


@contextlib.contextmanager
def active_channel(channel_id: Optional[str]):
    """Set the active channel for the duration of the block."""
    token = _ACTIVE_CHANNEL.set(channel_id or None)
    try:
        yield
    finally:
        _ACTIVE_CHANNEL.reset(token)


def current_channel() -> Optional[str]:
    """The active channel id, or None."""
    return _ACTIVE_CHANNEL.get()

_BRAND_BIBLE_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "brand_guardian"

# Finish nudges keyed off a small set of tone words. Deterministic.
_GENTLE_WORDS = ("gentle", "whimsical", "soothing", "calm", "cozy", "cosy",
                 "playful", "warm", "dreamy", "tender", "sweet")
_STARK_WORDS = ("investigative", "serious", "grave", "stark", "somber", "sombre",
                "forensic", "clinical", "hard-edged")


def _brand_profile(channel_id: str, db_path: Optional[Path]):
    try:
        from hypogeum.channel_config import get_brand_profile, get_channel_profile
        ch = get_channel_profile(channel_id, db_path=db_path)
        if not ch or not ch.brand_profile_id:
            return None
        return get_brand_profile(ch.brand_profile_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        return None


def _has_brand_bible(channel_id: str) -> bool:
    try:
        return (_BRAND_BIBLE_DIR / f"brand_bible_{channel_id}.json").is_file()
    except Exception:  # noqa: BLE001
        return False


def channel_uses_own_brand_profile(channel_id: Optional[str], db_path: Optional[Path] = None) -> bool:
    """True when `channel_id` should get a profile-derived identity rather
    than the canonical (Hypogeum bible) one."""
    if not channel_id or _has_brand_bible(channel_id):
        return False
    bp = _brand_profile(channel_id, db_path)
    return bool(bp and (getattr(bp, "visual_style", "") or "").strip())


# ---------------------------------------------------------------------------
# Profile-derived directives
# ---------------------------------------------------------------------------

def _finish_words(bp) -> str:
    tone = (getattr(bp, "tone", "") or "").lower()
    if any(w in tone for w in _GENTLE_WORDS):
        return "soft, warm, low-contrast, gentle"
    if any(w in tone for w in _STARK_WORDS):
        return "restrained, controlled, high-clarity, unembellished"
    return "restrained, natural, photographic"


_PROFILE_FORBIDDEN = [
    "generic fantasy-art finish",
    "glossy modern product-render look",
    "arbitrary teal-and-orange cinematic color grading",
    "artificial full-frame color wash not required by the scene",
    "neon color",
]


def _palette_phrase(bp) -> str:
    pal = getattr(bp, "color_palette", None) or {}
    if not isinstance(pal, dict) or not pal:
        return ""
    hexes = [str(v) for v in pal.values() if isinstance(v, str) and v.startswith("#")]
    if not hexes:
        return ""
    return " Palette anchors: " + ", ".join(sorted(hexes)[:4]) + "."


def _profile_directive(bp, channel_name: str, mode: str, level: str) -> str:
    style = (getattr(bp, "visual_style", "") or "").strip().rstrip(".")
    tone = (getattr(bp, "tone", "") or "").strip()
    label = f"{channel_name.upper()} BRAND VISUAL IDENTITY" if channel_name else "CHANNEL BRAND VISUAL IDENTITY"

    if level == "light":
        return (
            f" — {label} ({mode}, light enforcement): "
            f"Priority: (1) subject/entity continuity; (2) accuracy; (3) clear silhouette; "
            f"(4) neutral controlled rendering; (5) a subtle nudge toward this channel's look — "
            f"{style}. Finish: {_finish_words(bp)}. Avoid: {', '.join(_PROFILE_FORBIDDEN)}."
        )

    # adaptive (video_scene) and full share the same qualitative body; full
    # additionally asserts the style as a frame-wide target.
    body = (
        f" — {label} ({mode}, {level} enforcement): "
        f"Visual world: {style}."
        f"{_palette_phrase(bp)} "
        f"Finish: {_finish_words(bp)}. "
    )
    if tone:
        body += f"Mood: {tone}. "
    if level == "adaptive":
        body += (
            "Colors the scene truthfully requires (daylight, sky, sea, vegetation, "
            "fire, skin, clothing) take precedence over the palette. "
        )
    body += f"Avoid: {', '.join(_PROFILE_FORBIDDEN)}."
    return body


# ---------------------------------------------------------------------------
# Public router
# ---------------------------------------------------------------------------

def build_channel_visual_identity_directive(
    channel_id: Optional[str],
    mode: str = "generic",
    db_path: Optional[Path] = None,
) -> str:
    """Prompt-ready brand directive for `channel_id` in the given `mode`
    (same modes as visual_identity: video_scene / thumbnail / reference_image
    / generic ...). Falls back to the canonical Hypogeum identity when the
    channel has no brand profile of its own."""
    if not channel_uses_own_brand_profile(channel_id, db_path):
        return visual_identity.build_visual_identity_directive(mode)

    bp = _brand_profile(channel_id, db_path)
    try:
        from hypogeum.channel_config import get_channel_profile
        ch = get_channel_profile(channel_id, db_path=db_path)
        channel_name = (getattr(ch, "name", "") or channel_id or "").split(" ")[0]
    except Exception:  # noqa: BLE001
        channel_name = channel_id or ""

    level = visual_identity.get_enforcement_level(
        mode if mode in visual_identity._VALID_MODES else "generic")
    return _profile_directive(bp, channel_name, mode, level)


def build_channel_visual_identity_directive_compact(
    channel_id: Optional[str], mode: str = "generic", db_path: Optional[Path] = None,
) -> str:
    """Compact form. The profile-derived directive is already concise (no
    per-swatch palette tables), so this returns the same text as the full
    form for a channel with its own profile; otherwise it delegates to
    visual_identity's compact builder."""
    if not channel_uses_own_brand_profile(channel_id, db_path):
        return visual_identity.build_visual_identity_directive_compact(mode)
    return build_channel_visual_identity_directive(channel_id, mode, db_path)


def get_channel_adaptive_video_forbidden_items(
    channel_id: Optional[str], db_path: Optional[Path] = None,
) -> list:
    """FORBIDDEN items for the video_scene tier for `channel_id`."""
    if not channel_uses_own_brand_profile(channel_id, db_path):
        return visual_identity.get_adaptive_video_forbidden_items()
    return list(_PROFILE_FORBIDDEN)
