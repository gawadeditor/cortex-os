"""
hypogeum/brand/channel_thumbnail_identity.py — derive ThumbnailIdentityRules
for an arbitrary channel from its own BrandProfile.

The North Star: "Cortex Core contains ZERO channel knowledge."
`hypogeum.brand.thumbnail_identity_rules` is the *Hypogeum* channel's own
thumbnail contract — near-black void, warm gold accent, a "THE HYPOGEUM
CHRONICLES" wordmark, silhouette-only humans. The Thumbnail Identity Guardian
(Factory Core) resolves its rules through an injectable resolver, and its
default resolver used to return the Hypogeum contract for *every* channel —
so a bedtime-animation channel's thumbnails were graded against Hypogeum's
mystery aesthetic and always failed.

This module builds a `ThumbnailIdentityRules` for a non-Hypogeum channel
from its `BrandProfile` (visual_style, color_palette, persona/name). It is
deliberately permissive: a cloned channel that has not authored a detailed
thumbnail spec should not inherit Hypogeum's hard constraints. Returns
``None`` when the channel has no usable brand profile, in which case the
caller keeps its existing default (Hypogeum rules) — correct for the
Hypogeum channel itself and a safe no-op for a profile-less channel.

Public API:
    channel_thumbnail_identity_rules(channel_id, deliverable_class, *, db_path=None)
        -> Optional[ThumbnailIdentityRules]
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.brand.thumbnail_identity_rules import ThumbnailIdentityRules
from hypogeum.logger import get_logger

log = get_logger()

_DIMENSIONS = {
    "main_episode": ("16:9", "1280x720"),
    "short": ("9:16", "720x1280"),
}


def _palette_description(visual_style: str, color_palette: dict) -> str:
    hexes = [str(v) for v in (color_palette or {}).values() if str(v).startswith("#")]
    hex_note = f" Key colors: {', '.join(hexes)}." if hexes else ""
    style = visual_style.strip() if visual_style else "the channel's established look"
    return (
        f"Match this channel's visual style: {style}.{hex_note} "
        f"Evaluate the palette only against that style — do NOT require a dark, "
        f"near-black, or high-contrast background, and do NOT treat bright, warm, "
        f"pastel, or saturated color as a violation if it is consistent with the "
        f"style above."
    )


def channel_thumbnail_identity_rules(
    channel_id: str,
    deliverable_class: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[ThumbnailIdentityRules]:
    """ThumbnailIdentityRules for `channel_id` derived from its BrandProfile,
    or None when the channel has no brand profile (caller keeps its default).
    Never raises."""
    if deliverable_class not in _DIMENSIONS:
        return None
    try:
        from hypogeum.channel_config import get_brand_profile, get_channel_profile

        ch = get_channel_profile(channel_id, db_path=db_path)
        if not ch or not ch.brand_profile_id:
            return None
        bp = get_brand_profile(ch.brand_profile_id, db_path=db_path)
        if not bp:
            return None

        aspect, dims = _DIMENSIONS[deliverable_class]
        is_main = deliverable_class == "main_episode"
        channel_name = (ch.name or bp.name or "").strip()

        return ThumbnailIdentityRules(
            deliverable_class=deliverable_class,
            aspect_ratio=aspect,
            expected_dimensions=dims,
            # A cloned channel that has not authored a wordmark spec is not
            # required to stamp one — absence is never a violation here.
            wordmark_required=False,
            wordmark_text=channel_name or None,
            title_hierarchy_required=False,
            human_figure_policy="unrestricted",
            palette_description=_palette_description(bp.visual_style or "", bp.color_palette or {}),
            typography_description=(
                "No mandatory typeface. If the channel's brand profile names fonts, "
                "prefer them; otherwise any clean, legible treatment consistent with "
                f"the channel's style is acceptable. Text is optional on {deliverable_class} "
                "covers and its absence is never a violation."
            ),
            forbidden_style_language=[],
            focal_hierarchy_description=(
                "A single clear focal subject that reads at feed / search-result "
                f"thumbnail scale ({'~168x94px' if is_main else '~94x168px'}). "
                "Composition and mood should match the channel's own visual style."
            ),
            additional_rules_text=(
                f"These rules are derived from channel '{channel_id}'’s own brand "
                f"profile. Do NOT apply any other channel's aesthetic (e.g. a dark "
                f"cinematic-mystery look) unless this channel's style calls for it."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "channel_thumbnail_identity_error",
            channel_id=channel_id,
            deliverable_class=deliverable_class,
            error=str(exc),
        )
        return None
