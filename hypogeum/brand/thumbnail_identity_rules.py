"""
hypogeum/brand/thumbnail_identity_rules.py — The Hypogeum Chronicles'
own thumbnail identity rules, keyed by deliverable class.

This is Channel Package content, not Factory Core: hypogeum.agents.
thumbnail_identity_guardian imports this module through its stable
public function (get_thumbnail_identity_rules) exactly as every other
Factory Core agent already imports hypogeum.brand.visual_identity for
material/finish/color-philosophy content. A channel cloned from this
template replaces this file with its own rules; Factory Core never
hardcodes a channel's wordmark text, palette, dimensions, or which
deliverable classes exist -- it only knows how to ask this module for
"the rules for deliverable_class=X" and build a prompt from whatever
comes back.

Two classes are defined for this channel: 'main_episode' (the long-form
horizontal YouTube thumbnail) and 'short' (the vertical Short/Reel/
TikTok cover). A channel with a different deliverable taxonomy would
define its own set here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class ThumbnailIdentityRules:
    deliverable_class: str
    aspect_ratio: str                  # "16:9" | "9:16" | ...
    expected_dimensions: str           # human-readable, e.g. "1280x720"
    wordmark_required: bool
    wordmark_text: Optional[str]
    title_hierarchy_required: bool     # a headline/subtitle/wordmark 3-line structure
    human_figure_policy: str           # "silhouette_or_back_profile_only_no_frontal_face" | "prohibited" | "unrestricted"
    palette_description: str
    typography_description: str
    forbidden_style_language: List[str] = field(default_factory=list)
    focal_hierarchy_description: str = ""
    additional_rules_text: str = ""


_SHARED_PALETTE = (
    "Background: dark (void_black #080C0E or deep_stone #1A1D20)-family tones. "
    "Must read 0-40% brightness at native resolution. Never bright, high-key, or "
    "light. Warm gold/bronze accent lighting on the focal subject."
)
_SHARED_FORBIDDEN_STYLE = [
    "neon", "cyberpunk", "sci-fi", "modern visual language",
    "bright white fills", "cool blues", "greens", "pinks", "purples", "bright oranges",
]
_SHARED_HUMAN_FIGURE_POLICY = "silhouette_or_back_profile_only_no_frontal_face"

MAIN_EPISODE_RULES = ThumbnailIdentityRules(
    deliverable_class="main_episode",
    aspect_ratio="16:9",
    expected_dimensions="1280x720",
    wordmark_required=True,
    wordmark_text="THE HYPOGEUM CHRONICLES",
    title_hierarchy_required=True,
    human_figure_policy=_SHARED_HUMAN_FIGURE_POLICY,
    palette_description=_SHARED_PALETTE,
    typography_description=(
        "The lower-left text block is a two-line stack, per this channel's own "
        "thumbnail_style_system.md §4.2 word hierarchy: Line 1 (headline, "
        "the setup, 3-4 words, smaller, Regular/SemiBold weight) directly above "
        "Line 2 (emphasis, the payload, 1-2 words, much larger, Bold/ExtraBold "
        "weight, the first word the viewer reads). This two-line headline+"
        "emphasis stack is the complete, correct structure on its own -- there "
        "is no separate third 'subtitle' line anywhere in this channel's "
        "layout, and its absence must never be flagged as a violation."
    ),
    forbidden_style_language=list(_SHARED_FORBIDDEN_STYLE),
    focal_hierarchy_description=(
        "A single clear focal subject, the small wordmark lockup near the top, "
        "and the two-line headline+emphasis text stack (described under "
        "Typography above) anchored lower-left -- together these three "
        "elements (subject / wordmark / headline+emphasis stack) form the "
        "complete hierarchy. It must be readable at YouTube search-result "
        "thumbnail scale (~168x94px)."
    ),
)

SHORT_RULES = ThumbnailIdentityRules(
    deliverable_class="short",
    aspect_ratio="9:16",
    expected_dimensions="720x1280",
    wordmark_required=False,
    wordmark_text=None,
    title_hierarchy_required=False,
    human_figure_policy=_SHARED_HUMAN_FIGURE_POLICY,
    palette_description=_SHARED_PALETTE,
    typography_description=(
        "No mandatory text overlay -- a Short cover may be image-only. If text "
        "is present it should use the same Cinzel/Raleway family as the main "
        "episode, but its absence is never a violation on its own."
    ),
    forbidden_style_language=list(_SHARED_FORBIDDEN_STYLE),
    focal_hierarchy_description=(
        "A single unambiguous focal subject occupying the vertical frame's "
        "center band, readable at mobile Shorts/Reels/TikTok feed scale "
        "(~94x168px) without any wordmark or headline required to carry "
        "meaning -- the image itself must read clearly at that scale."
    ),
)

_RULES_BY_CLASS = {
    "main_episode": MAIN_EPISODE_RULES,
    "short": SHORT_RULES,
}


def get_thumbnail_identity_rules(deliverable_class: str) -> ThumbnailIdentityRules:
    """
    The only entry point Factory Core is expected to call. Raises
    ValueError for an unknown deliverable_class rather than silently
    falling back to another class's rules -- an unconfigured class must
    never be evaluated against the wrong contract.
    """
    try:
        return _RULES_BY_CLASS[deliverable_class]
    except KeyError:
        raise ValueError(
            f"no thumbnail identity rules configured for deliverable_class={deliverable_class!r} "
            f"-- known classes: {sorted(_RULES_BY_CLASS)}"
        )
