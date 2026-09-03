"""
hypogeum/caption_style.py — how burned-in captions look on a short-native
vertical deliverable, and where they sit inside the 9:16 safe area.

Two things live here and nowhere else:
  1. CaptionStyle — the font / size / weight / colour / position knobs an
     operator sets per channel.
  2. The 9:16 safe-area model — the fraction of a 1080x1920 frame that the
     platforms (TikTok / Reels / Shorts) cover with their own UI, which the
     caption block must stay clear of.

`resolve_caption_style(channel_id, db_path)` reads the channel's
`caption_style` override (PublishingProfile / channel package) and merges
it onto the short-native default. `build_ass_style(...)` turns a
CaptionStyle + a concrete WxH into the ASS `[V4+ Styles]` line + the
`MarginV` the renderer bakes into the .ass file.

The renderer (`hypogeum/agents/video_renderer.py`) is the only caller.
Nothing here shells out or touches ffmpeg — it only produces strings.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 9:16 safe area (fractions of frame height / width the platform UI covers)
# ---------------------------------------------------------------------------
# Measured against TikTok / Instagram Reels / YouTube Shorts overlays on a
# 1080x1920 canvas. The caption block is anchored inside the band between
# SAFE_TOP and (1 - SAFE_BOTTOM); the right rail (like / comment / share
# buttons) eats SAFE_RIGHT, so captions also get a right margin.
SAFE_TOP_FRAC    = 0.10   # status bar + "Following / For You"
SAFE_BOTTOM_FRAC = 0.18   # username, caption text, progress bar, nav
SAFE_SIDE_FRAC   = 0.055  # left gutter
SAFE_RIGHT_FRAC  = 0.16   # right action rail

_POSITIONS = ("bottom", "lower-third", "center", "top")

# ASS alignment (numpad): 2 = bottom-center, 5 = middle-center, 8 = top-center
_ALIGN = {"bottom": 2, "lower-third": 2, "center": 5, "top": 8}


@dataclass(frozen=True)
class CaptionStyle:
    """Per-channel burned-caption appearance. All sizes are for a 1080-wide
    frame and scale linearly with real width.

    `font` must be a family libass can resolve on the render host via
    fontconfig. The default is a face present on stock macOS; a
    containerised factory should bundle its brand font and set it here."""
    font: str = "Helvetica Neue"      # stock-macOS safe; has a Bold weight
    font_size: int = 64               # px at 1080 wide
    bold: bool = True
    position: str = "lower-third"     # bottom | lower-third | center | top
    max_words_per_line: int = 3
    primary_hex: str = "FFFFFF"       # caption fill (RRGGBB)
    outline_hex: str = "000000"       # stroke
    outline_px: int = 6               # heavy stroke reads on any footage
    shadow_px: int = 0
    # extra clearance (px @1080w) added on top of the platform safe area,
    # e.g. to lift text above a channel's own end-card lower band
    extra_margin_v: int = 24
    uppercase: bool = False

    def validated(self) -> "CaptionStyle":
        pos = self.position if self.position in _POSITIONS else "lower-third"
        return replace(
            self,
            font=(self.font or "Montserrat").strip(),
            font_size=max(24, min(int(self.font_size or 64), 160)),
            position=pos,
            max_words_per_line=max(1, min(int(self.max_words_per_line or 3), 8)),
            primary_hex=_clean_hex(self.primary_hex, "FFFFFF"),
            outline_hex=_clean_hex(self.outline_hex, "000000"),
            outline_px=max(0, min(int(self.outline_px or 0), 24)),
            shadow_px=max(0, min(int(self.shadow_px or 0), 24)),
            extra_margin_v=max(0, min(int(self.extra_margin_v or 0), 600)),
        )

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        """One line an operator can read: 'Montserrat 64px bold · lower third'."""
        w = "bold" if self.bold else "regular"
        return f"{self.font} {self.font_size}px {w} · {self.position.replace('-', ' ')}"


SHORT_NATIVE_DEFAULT = CaptionStyle()


def _clean_hex(v, fallback: str) -> str:
    s = str(v or "").strip().lstrip("#").upper()
    if len(s) == 6 and all(c in "0123456789ABCDEF" for c in s):
        return s
    return fallback


def _ass_colour(rrggbb: str) -> str:
    """RRGGBB -> ASS &HAABBGGRR& (opaque)."""
    rr, gg, bb = rrggbb[0:2], rrggbb[2:4], rrggbb[4:6]
    return f"&H00{bb}{gg}{rr}"


def merge_style(override: Optional[dict]) -> CaptionStyle:
    """Merge a stored dict override onto the short-native default. Unknown
    keys are ignored; a missing / empty dict returns the default."""
    if not override or not isinstance(override, dict):
        return SHORT_NATIVE_DEFAULT
    fields = SHORT_NATIVE_DEFAULT.as_dict()
    for k, v in override.items():
        if k in fields and v is not None:
            fields[k] = v
    try:
        return CaptionStyle(**fields).validated()
    except Exception:  # noqa: BLE001
        return SHORT_NATIVE_DEFAULT


def _publishing_profile_for_channel(channel_id: str, db_path: Optional[Path]):
    from hypogeum.channel_config import get_channel_profile, get_publishing_profile
    cp = get_channel_profile(channel_id=channel_id, db_path=db_path)
    if cp is None or not getattr(cp, "publishing_profile_id", None):
        return None
    return get_publishing_profile(cp.publishing_profile_id, db_path=db_path)


def resolve_caption_style(channel_id: Optional[str],
                          db_path: Optional[Path] = None) -> CaptionStyle:
    """The CaptionStyle in force for a channel — its PublishingProfile
    `caption_style` override merged onto SHORT_NATIVE_DEFAULT. Never raises."""
    if not channel_id:
        return SHORT_NATIVE_DEFAULT
    try:
        pub = _publishing_profile_for_channel(channel_id, db_path)
        return merge_style(getattr(pub, "caption_style", None) if pub else None)
    except Exception:  # noqa: BLE001
        return SHORT_NATIVE_DEFAULT


def burn_enabled(channel_id: Optional[str], db_path: Optional[Path] = None) -> bool:
    """True when this channel wants captions burned into the video pixels
    (PublishingProfile.auto_captions). Default False — a channel opts in."""
    if not channel_id:
        return False
    try:
        pub = _publishing_profile_for_channel(channel_id, db_path)
        return bool(getattr(pub, "auto_captions", False)) if pub else False
    except Exception:  # noqa: BLE001
        return False


def safe_area_margins(width: int, height: int) -> dict:
    """Pixel margins the platform UI reserves on a WxH vertical frame."""
    return {
        "top": round(height * SAFE_TOP_FRAC),
        "bottom": round(height * SAFE_BOTTOM_FRAC),
        "left": round(width * SAFE_SIDE_FRAC),
        "right": round(width * SAFE_RIGHT_FRAC),
    }


def build_ass_style(style: CaptionStyle, width: int, height: int) -> dict:
    """Return everything the .ass writer needs:
        {style_line, margin_l, margin_r, margin_v, alignment,
         play_res_x, play_res_y, uppercase, max_words_per_line}
    Sizes in `style` are authored for 1080 wide and scaled to `width`."""
    style = style.validated()
    scale = width / 1080.0
    fs = max(12, round(style.font_size * scale))
    outline = max(0, round(style.outline_px * scale))
    shadow = max(0, round(style.shadow_px * scale))

    safe = safe_area_margins(width, height)
    align = _ALIGN.get(style.position, 2)
    extra = round(style.extra_margin_v * scale)

    if align == 2:            # bottom-anchored: MarginV measured from the bottom
        margin_v = safe["bottom"] + extra
    elif align == 8:          # top-anchored: from the top
        margin_v = safe["top"] + extra
    else:                     # centred: MarginV is ignored by libass, keep 0
        margin_v = 0

    margin_l = max(safe["left"], round(60 * scale))
    margin_r = max(safe["right"], round(60 * scale))

    style_line = (
        "Style: Default,"
        f"{style.font},{fs},"
        f"{_ass_colour(style.primary_hex)},"
        "&H000000FF,"                       # SecondaryColour (unused)
        f"{_ass_colour(style.outline_hex)},"
        "&H64000000,"                       # BackColour (semi-shadow, BorderStyle 1)
        f"{1 if style.bold else 0},0,0,0,"  # Bold,Italic,Underline,StrikeOut
        "100,100,0,0,"                      # ScaleX,ScaleY,Spacing,Angle
        "1,"                                # BorderStyle: outline+shadow (no box)
        f"{outline},{shadow},"
        f"{align},"
        f"{margin_l},{margin_r},{margin_v},1"
    )
    return {
        "style_line": style_line,
        "margin_l": margin_l,
        "margin_r": margin_r,
        "margin_v": margin_v,
        "alignment": align,
        "play_res_x": width,
        "play_res_y": height,
        "uppercase": style.uppercase,
        "max_words_per_line": style.max_words_per_line,
    }
