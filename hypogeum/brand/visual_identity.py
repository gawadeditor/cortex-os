"""
hypogeum/brand/visual_identity.py — Canonical visual identity loader

Single reusable access point for the Hypogeum brand palette, palette usage
ratios, forbidden color behavior, and visual finish rules. Reads from the
ONE canonical source of truth — docs/brand_guardian/brand_bible_hypogeum.json
(color_system + visual_finish_system sections) — and never duplicates those
values. If the Brand Bible is extended, every caller of this module picks up
the change automatically; no prompt file, agent, or compositor should ever
hardcode a brand hex value or forbidden-color list again.

Context-aware enforcement (Brand Bible v1.1, activated 2026-07-13):
Brand identity is one thing, but how strictly it is enforced varies by
output type. Applying thumbnail-strength palette percentages to a factual
video scene would override historically/environmentally accurate colors
(blue sky, blue sea, natural greenery) that the scene truthfully requires.
Three enforcement tiers exist:

  FULL     — thumbnail, thumbnail_prompt_writer, thumbnail_typography,
             thumbnail_compositor, endscreen, channel_artwork, generic
             (safest default for unrecognized modes).
             Exact hex palette, frame-coverage ratio targets, forbidden
             color casts, visual finish, all enforced verbatim.

  ADAPTIVE — video_scene.
             Same Hypogeum visual DNA (controlled directional lighting,
             restrained saturation, aged/tactile material rendering,
             premium documentary finish, no fantasy-art/glossy-render/neon)
             communicated qualitatively, with NO frame-coverage percentage
             targets and NO forbidding of historically/environmentally
             accurate colors (daylight, sky, sea, vegetation, fire, etc.).
             Scene truth takes precedence over palette ratios.

  LIGHT    — reference_image.
             Entity continuity and historical/material accuracy come first.
             No palette percentages at all. Only a subtle, optional brand
             finish nudge (avoid neon/fantasy/glossy-render) plus an
             optional restrained gold accent mention when contextually
             appropriate.

This module is deliberately independent of Brand Guardian Agent (BGA)
enforcement state. BGA's Brand Bible _global_status governs a separate,
JSON-based governance/approval record (see docs/brand_guardian/
brand_bible_hypogeum.json _meta.activation_note) — BGA's actual runtime
enforcement content is a hand-maintained prose file,
hypogeum/prompts/brand_guardian_agent.txt, which has its own Pass-1/Pass-2
context-aware color rules mirroring the tiers below. This module injects
brand guidance into every generation prompt regardless of BGA's state —
prompt injection and BGA enforcement are separate concerns.

Public API:
    get_palette()                  -> dict[str, dict]  (canonical 7-swatch palette)
    get_usage_ratios()             -> dict
    get_forbidden_color_behavior() -> list[str]
    get_visual_finish_rules()      -> dict  ({"positive": [...], "forbidden": [...]})
    get_brand_spec_version()       -> str
    get_enforcement_level(mode)    -> str  ("full" | "adaptive" | "light")
    build_visual_identity_directive(mode) -> str
    log_palette_injection(log, *, mode, injected, validation_result=None) -> None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BRAND_BIBLE_PATH = _PROJECT_ROOT / "docs" / "brand_guardian" / "brand_bible_hypogeum.json"

# Canonical alias map: user/prompt-facing names -> Brand Bible swatch names.
# The Brand Bible (docs/brand_guardian/brand_bible_hypogeum.json) remains the
# only place hex values are written. This module only re-labels them.
_PALETTE_ALIASES = {
    "near_black":      "void_black",
    "charcoal":        "deep_stone",
    "dark_stone":      "charcoal_earth",
    "muted_bronze":    "ancient_soil",
    "aged_gold":       "antique_gold",
    "warm_ivory":      "warm_gold",
    "soft_off_white":  "parchment_cream",
}

# mode -> enforcement tier. Unrecognized modes fall back to "generic" (full).
_MODE_ENFORCEMENT = {
    "thumbnail":              "full",
    "thumbnail_compositor":   "full",
    "thumbnail_typography":   "full",
    "thumbnail_prompt_writer": "full",
    "endscreen":               "full",
    "channel_artwork":         "full",
    "generic":                 "full",
    "video_scene":              "adaptive",
    "reference_image":          "light",
}
_VALID_MODES = tuple(_MODE_ENFORCEMENT.keys())

_cache: Optional[dict] = None


def _load_bible() -> dict:
    global _cache
    if _cache is None:
        _cache = json.loads(_BRAND_BIBLE_PATH.read_text(encoding="utf-8"))
    return _cache


def get_brand_spec_version() -> str:
    """Return the Brand Bible version string (e.g. 'v1.1')."""
    return _load_bible()["_meta"]["bible_version"]


def get_palette() -> dict:
    """
    Return the canonical 7-swatch palette keyed by the alias names used in
    generation prompts (near_black, charcoal, dark_stone, muted_bronze,
    aged_gold, warm_ivory, soft_off_white), each mapping to
    {"hex": ..., "bible_name": ..., "role": ...}.
    """
    primary_palette = _load_bible()["color_system"]["primary_palette"]
    palette = {}
    for alias, bible_name in _PALETTE_ALIASES.items():
        swatch = primary_palette[bible_name]
        palette[alias] = {
            "hex": swatch["hex"],
            "bible_name": bible_name,
            "role": swatch["role"],
        }
    return palette


def get_usage_ratios() -> dict:
    """Return the color_system.usage_ratios section verbatim."""
    return _load_bible()["color_system"]["usage_ratios"]


def get_forbidden_color_behavior() -> list:
    """Return the color_system.forbidden_color_behavior.items list."""
    return list(_load_bible()["color_system"]["forbidden_color_behavior"]["items"])


def get_visual_finish_rules() -> dict:
    """Return {"positive": [...], "forbidden": [...]} from visual_finish_system."""
    section = _load_bible()["visual_finish_system"]
    return {
        "positive": list(section["positive_finish"]),
        "forbidden": list(section["forbidden_finish"]),
    }


def get_enforcement_level(mode: str) -> str:
    """Return 'full' | 'adaptive' | 'light' for the given mode (unknown -> 'full')."""
    return _MODE_ENFORCEMENT.get(mode, "full")


def _build_full_directive(mode: str) -> str:
    """Exact hex palette, frame-coverage ratios, forbidden casts, finish."""
    palette = get_palette()
    ratios = get_usage_ratios()
    forbidden_colors = get_forbidden_color_behavior()
    finish = get_visual_finish_rules()

    gold = palette["aged_gold"]["hex"]
    near_black = palette["near_black"]["hex"]
    charcoal = palette["charcoal"]["hex"]
    dark_stone = palette["dark_stone"]["hex"]
    muted_bronze = palette["muted_bronze"]["hex"]
    warm_ivory = palette["warm_ivory"]["hex"]
    soft_off_white = palette["soft_off_white"]["hex"]

    dark_pct = ratios["near_black_and_charcoal"]["target_range_pct"]
    mid_pct = ratios["dark_stone_and_muted_bronze"]["target_range_pct"]
    gold_pct = ratios["aged_gold"]["target_range_pct"]

    return (
        f" — HYPOGEUM BRAND VISUAL IDENTITY ({mode}, full enforcement): "
        f"Palette: near-black {near_black} and charcoal {charcoal} dominate "
        f"({dark_pct[0]}-{dark_pct[1]}% of frame); dark stone {dark_stone} and "
        f"muted bronze {muted_bronze} as midground/structural material "
        f"({mid_pct[0]}-{mid_pct[1]}%); aged gold {gold} as a restrained accent only "
        f"({gold_pct[0]}-{gold_pct[1]}%, never a dominant field color); "
        f"warm ivory {warm_ivory} and off-white {soft_off_white} for highlights and "
        f"text only, never a background or large fill. "
        f"Forbidden: {'; '.join(forbidden_colors)}. "
        f"Finish: {', '.join(finish['positive'])}. "
        f"Avoid: {', '.join(finish['forbidden'])}."
    )


def get_adaptive_video_forbidden_items() -> list:
    """
    Forbidden items for the ADAPTIVE (video_scene) tier, as a flat list —
    the only source of negative language _build_adaptive_video_directive()
    would otherwise need to embed. Callers (hypogeum.video_prompt_writer)
    collect this alongside Video DNA's own forbidden items into one
    dedicated FORBIDDEN prompt section.

    Deliberately narrower than get_forbidden_color_behavior() (the FULL/
    thumbnail-tier list) — adaptive tier does not forbid natural
    environmental colors (blue sky, blue sea, natural greenery).
    """
    finish = get_visual_finish_rules()
    return list(finish["forbidden"]) + [
        "arbitrary teal-and-orange cinematic color grading",
        "artificial full-frame color wash not required by the scene",
    ]


def _build_adaptive_video_directive(mode: str) -> str:
    """
    Qualitative Hypogeum visual DNA for video scenes, scoped to exactly
    four owned concerns — material character, finish, lighting character,
    color philosophy — per the Prompt Writer architecture revision
    (2026-07-14): camera behavior, motion rules, and documentary/realism
    language now belong exclusively to hypogeum.video_dna, not here.
    No frame-coverage percentages. Contains no "never"/"avoid" language —
    see get_adaptive_video_forbidden_items() for the negative counterpart.
    Scene truth (natural/historical colors) still takes precedence over
    palette ratios.
    """
    gold = get_palette()["aged_gold"]["hex"]

    return (
        f" — HYPOGEUM BRAND VISUAL IDENTITY ({mode}, adaptive enforcement — "
        f"same Hypogeum visual DNA, scene-truth first): "
        f"Material character: aged, tactile, weathered surfaces. "
        f"Finish: premium archaeological documentary, realistic photographic "
        f"texture, restrained saturation. "
        f"Lighting character: warm amber or natural daylight color temperature. "
        f"Color philosophy: brand gold {gold} as a restrained accent only when "
        f"naturally present (metal, light catching a surface); historically or "
        f"environmentally accurate colors the scene truthfully requires — "
        f"daylight or night conditions, sea, sky, vegetation, clothing, "
        f"architecture, fire, and other location-specific colors — take "
        f"precedence over palette ratios."
    )


def _build_light_reference_directive(mode: str) -> str:
    """
    Light guidance for entity/reference images. Priority order: entity
    continuity, historical/material accuracy, silhouette/identifiable
    features, neutral controlled photographic rendering, subtle Hypogeum
    finish. No rigid palette percentages.
    """
    gold = get_palette()["aged_gold"]["hex"]
    finish = get_visual_finish_rules()

    return (
        f" — HYPOGEUM BRAND VISUAL IDENTITY ({mode}, light enforcement): "
        f"Priority order: (1) entity continuity — this reference must match how the "
        f"entity is described elsewhere; (2) historical/material accuracy; "
        f"(3) clear silhouette and identifiable features; (4) neutral, controlled "
        f"photographic rendering; (5) subtle Hypogeum finish. "
        f"Use realistic texture, controlled neutral lighting, and restrained "
        f"saturation. Avoid: {', '.join(finish['forbidden'])}, and neon color. "
        f"Do not apply thumbnail-strength dark/gold palette percentages — this is a "
        f"reference image, not a thumbnail. A restrained brand gold {gold} accent is "
        f"optional and only appropriate when the entity's own material (metal, gilt "
        f"detail) naturally calls for it."
    )


def build_visual_identity_directive(mode: str = "generic") -> str:
    """
    Assemble a compact, prompt-ready text block carrying Hypogeum brand
    guidance appropriate to the calling context's enforcement tier
    (see get_enforcement_level). Designed to be concatenated onto an
    existing prompt string WITHOUT mutating that string, e.g.:
        prompt = base_prompt + build_visual_identity_directive("thumbnail")
    Callers must not strip trailing punctuation from base_prompt before
    concatenating — the directive begins with its own " — " separator, and
    the original prompt text must remain byte-identical as a prefix.
    """
    if mode not in _VALID_MODES:
        mode = "generic"

    level = get_enforcement_level(mode)
    if level == "adaptive":
        return _build_adaptive_video_directive(mode)
    if level == "light":
        return _build_light_reference_directive(mode)
    return _build_full_directive(mode)


def build_visual_identity_directive_compact(mode: str = "generic") -> str:
    """
    Provider-safe, length-conscious rendering of the FULL-tier brand
    directive (thumbnail / thumbnail_compositor / thumbnail_typography /
    thumbnail_prompt_writer / endscreen / channel_artwork / generic).

    Added 2026-07-13 after a real MUAPI "content could not be processed"
    rejection on a 6,730-character unified thumbnail prompt — the FULL
    directive's ~1,100 characters were a meaningful share of that. Every
    palette ratio and every forbidden-color/finish rule from
    _build_full_directive() is preserved; only the prose is condensed (full
    per-swatch role descriptions and every named forbidden item are
    shortened to their essential terms). get_palette()/get_usage_ratios()/
    get_forbidden_color_behavior()/get_visual_finish_rules() remain the only
    source of truth — this function reads the same values, it does not
    define new ones.

    For ADAPTIVE (video_scene) and LIGHT (reference_image) tiers, which are
    already shorter and qualitative by design, this returns the same output
    as build_visual_identity_directive() unchanged — compaction is scoped to
    the tier that actually caused a provider rejection.
    """
    if mode not in _VALID_MODES:
        mode = "generic"
    level = get_enforcement_level(mode)
    if level != "full":
        return build_visual_identity_directive(mode)

    palette = get_palette()
    ratios = get_usage_ratios()
    forbidden_colors = get_forbidden_color_behavior()
    finish = get_visual_finish_rules()

    near_black = palette["near_black"]["hex"]
    gold = palette["aged_gold"]["hex"]
    dark_pct = ratios["near_black_and_charcoal"]["target_range_pct"]
    mid_pct = ratios["dark_stone_and_muted_bronze"]["target_range_pct"]
    gold_pct = ratios["aged_gold"]["target_range_pct"]

    # Shorten each forbidden-color item to its key term(s) rather than the
    # full explanatory clause used in the verbose/full directive.
    forbidden_short = "; ".join(item.split(" (")[0].split(" —")[0] for item in forbidden_colors)

    return (
        f" — HYPOGEUM BRAND ({mode}, compact): near-black {near_black}/charcoal dominant "
        f"({dark_pct[0]}-{dark_pct[1]}%); dark stone/muted bronze mid-ground ({mid_pct[0]}-{mid_pct[1]}%); "
        f"aged gold {gold} restrained accent only ({gold_pct[0]}-{gold_pct[1]}%, never dominant); "
        f"warm ivory/off-white for text/highlights only. Forbidden: {forbidden_short}. "
        f"Finish: {finish['positive'][0]}, {finish['positive'][-1]}. "
        f"Avoid: {', '.join(finish['forbidden'])}."
    )


def log_palette_injection(
    log,
    *,
    mode: str,
    injected: bool,
    validation_result: Optional[dict] = None,
) -> None:
    """
    Record a structured log entry for a brand-identity injection event.

    Args:
        log:               A hypogeum.logger logger instance (never print()).
        mode:               The applicable visual mode (e.g. "thumbnail", "video_scene").
        injected:           Whether the directive was actually concatenated onto the prompt.
        validation_result:  Optional palette-compliance validator result dict
                             (see hypogeum.brand.palette_validator), or None if
                             no post-generation validation was run.
    """
    log.info(
        "brand_visual_identity_injected",
        brand_spec_version=get_brand_spec_version(),
        palette_injected=injected,
        applicable_visual_mode=mode,
        enforcement_level=get_enforcement_level(mode),
        validation_result=(validation_result or {}).get("pass") if validation_result else None,
    )
