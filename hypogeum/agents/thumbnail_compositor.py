"""
hypogeum/agents/thumbnail_compositor.py — Multi-layer thumbnail compositor

Thumbnail mode registry
-----------------------
THUMBNAIL_MODE_UNIFIED_INTEGRATED = "unified_integrated"
    APPROVED PRIMARY PATH (Thumbnail DNA v1, 2026-07-13). One unified GPT
    Image 2 call with integrated typography baked into the same prompt —
    no layered base/hero/FX calls, no PIL headline. The final provider
    prompt is built deterministically from exactly four components, each
    present exactly once: (1) candidate["image_prompt"] topic-specific
    concept, (2) integrated typography instructions, (3) the canonical
    Thumbnail DNA directive, (4) the canonical FULL brand visual-identity
    directive. Use generate_unified_integrated_thumbnail(). Wordmark is
    still applied via PIL afterward (TIG-07 requirement).

THUMBNAIL_MODE_PREMIUM  = "multi_layer_composite"
    Layered path. Preserved, not removed — no longer the default for new
    concept-driven candidates as of Thumbnail DNA v1, but still fully
    functional for rollback or manual override.
    Generates 3 visual layers via MUAPI, renders typography + wordmark via PIL.
    Required layers: base_scene, hero_subject, fx  (MUAPI)
                     typography                     (PIL — deterministic)
                     wordmark                       (PIL — mandatory final step)
    Use generate_and_composite() with candidate["composite_mode"] = True.

THUMBNAIL_MODE_FAST     = "single_pass_integrated"
    Legacy single-call mode reached only via the LLM-authored (non-concept-
    driven) image_prompt path in thumbnail_generation_agent.py's
    _generate_for_deliverable(). Superseded for concept-driven candidates by
    THUMBNAIL_MODE_UNIFIED_INTEGRATED, which is deterministic rather than
    relying on the LLM to have embedded DNA/brand guidance itself.

THUMBNAIL_MODE_LEGACY   = "pil_composited_text"
    Fallback only. Single image generation + PIL text overlay (old behaviour).
    Only reached when typography_plan is present but composite_mode is False
    and typography_integration != "embedded".

Layered compositing workflow (PREMIUM only):
  Layer 1 — base_scene:    Environment plate only (no subject, no text) — MUAPI
  Layer 2 — hero_subject:  Isolated subject on dark background          — MUAPI
  Layer 3 — fx:            Atmospheric effects on black (screen blended) — MUAPI
  Layer 4 — typography:    Hook text on black — PIL-rendered, deterministic
  → PIL composite → color grade → vignette → wordmark overlay

Public API:
    build_layer_prompts(concept, composition_plan) -> dict
    generate_and_composite(candidate, config, episode_id, output_dir, deliverable_id, log) -> bool
    generate_unified_integrated_thumbnail(candidate, config, episode_id, output_dir, deliverable_id, log) -> bool
    validate_premium_thumbnail(output_dir, deliverable_id) -> dict
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.muapi import _generate_thumbnail_with_model, _normalize_thumbnail
from hypogeum.brand import visual_identity
from hypogeum.agents.thumbnail_composition_planner import (
    describe_dna_constraints,
    describe_dna_constraints_compact,
)


# ---------------------------------------------------------------------------
# Thumbnail mode constants
# ---------------------------------------------------------------------------

THUMBNAIL_MODE_UNIFIED_INTEGRATED = "unified_integrated"  # approved primary path
THUMBNAIL_MODE_PREMIUM = "multi_layer_composite"    # layered path — preserved, not default
THUMBNAIL_MODE_FAST    = "single_pass_integrated"   # legacy LLM-authored single-call mode
THUMBNAIL_MODE_LEGACY  = "pil_composited_text"      # fallback only


# ---------------------------------------------------------------------------
# Layer constants
# ---------------------------------------------------------------------------

LAYER_NAMES  = ("base_scene", "hero_subject", "fx", "typography")
LAYERS_SUBDIR = "layers"


# ---------------------------------------------------------------------------
# Layer prompt builder — deterministic from concept data
# ---------------------------------------------------------------------------

def build_layer_prompts(concept: dict, composition_plan: dict) -> dict:
    """
    Build three MUAPI layer prompts from a concept dict.

    Each prompt is optimised for its single job:
      base_scene   — environment only, no subject, no text
      hero_subject — subject only on dark background (screen blend ready)
      fx           — atmospheric particles/light on black (screen blend)

    Typography is NOT included — it is rendered deterministically via PIL
    by _render_typography_pil(), never via MUAPI.

    Args:
        concept:          One concept dict from thumbnail_concept_brief.concepts[].
        composition_plan: composition_plan from plan_composition().

    Returns:
        dict with keys: base_scene, hero_subject, fx
    """
    hook_text   = concept.get("suggested_hook_text", "").strip()
    visual_thesis = concept.get("visual_thesis", "")
    trigger     = concept.get("emotional_trigger", "")
    env_level   = composition_plan.get("environment_level", "contextual")

    # Split hook into headline / emphasis (same logic as typography_planner)
    words = hook_text.split()
    if len(words) <= 4:
        headline  = " ".join(words[:-1]) if len(words) > 1 else ""
        emphasis  = words[-1] if words else ""
    else:
        headline  = " ".join(words[:-2])
        emphasis  = " ".join(words[-2:])

    # Environment description keyed to trigger
    env_descs = {
        "impossibility": (
            "Dark ancient underground stone hypogeum chamber. "
            "Rough irregular stone walls. Faint astronomical diagrams and carved geometric "
            "patterns scratched into rock surface in the background. "
            "Ancient wooden shelves with clay pots and bronze instruments. "
            "Scattered tools and fragments on the ground. "
            "Warm amber torchlight from upper left. Volumetric dust. "
            "No mechanism visible. No text. Pure atmospheric environment plate."
        ),
        "loss": (
            "Dark crumbling stone archive chamber. "
            "Collapsed stone shelves. Empty niches where scrolls once rested. "
            "Fading astronomical diagrams barely visible on eroded walls. "
            "A single hanging lantern provides dim amber light. "
            "Dust, age, absence. No text. Pure atmospheric environment plate."
        ),
        "betrayal": (
            "Dark stone workshop or vault. "
            "A rough wooden workbench. Ancient stone walls. "
            "Warm amber light from an oil lamp. "
            "Shadowed tools. Mystery and concealment. "
            "No subject visible. No text. Pure atmospheric environment plate."
        ),
    }
    env_prompt = env_descs.get(trigger, env_descs["impossibility"])

    # Subject description keyed to trigger
    subject_descs = {
        "impossibility": (
            "Massive ancient Antikythera-like astronomical mechanism on dark stone surface. "
            "Enormous interlocking bronze gear wheels, circular dials with precision engraving, "
            "corroded green-gold patina, mechanical complexity. "
            "Warm amber rim light defines the gears. Deep dark background. "
            "Object fills 70-80% of the frame. Sharp and detailed. "
            "No text anywhere. No other objects."
        ),
        "loss": (
            "Ancient astronomical diagram carved in stone, partially erased and crumbling. "
            "Faint star charts dissolving into darkness from right to left. "
            "A fragment of bronze mechanism barely visible at the edge. "
            "Warm lantern light from right. Dark background. "
            "No text. The trace of something that once existed."
        ),
        "betrayal": (
            "Anonymous robed or hooded human figure, completely faceless, back or profile turned away. "
            "Leaning over a precision bronze gear mechanism on a wooden surface. "
            "Only the figure's silhouette and hands visible in warm oil lamp light. "
            "The face is fully in shadow or turned away — identity completely hidden. "
            "Dark stone background. No text."
        ),
    }
    subject_prompt = subject_descs.get(trigger, subject_descs["impossibility"])

    # FX layer — atmospheric effects on black for screen compositing
    fx_prompt = (
        "Atmospheric dust particles floating in warm amber torchlight beams. "
        "Volumetric god rays. Gold motes suspended in air. "
        "Wisps of smoke. Warm orange-gold particles. "
        "Pure black background. For screen-blend compositing. "
        "No subjects, no text, only atmosphere and particles."
    )

    directive = visual_identity.build_visual_identity_directive(mode="thumbnail_compositor")
    dna_directive = describe_dna_constraints(composition_plan)

    return {
        "base_scene":   env_prompt + dna_directive + directive,
        "hero_subject": subject_prompt + dna_directive + directive,
        "fx":           fx_prompt + directive,
    }


# ---------------------------------------------------------------------------
# PIL typography rendering — deterministic, no MUAPI
# ---------------------------------------------------------------------------

_WORDMARK_LINE1 = "THE"
_WORDMARK_LINE2 = "HYPOGEUM"
_WORDMARK_LINE3 = "— CHRONICLES —"


def _hex_to_rgb(hex_str: str) -> tuple:
    hex_str = hex_str.lstrip("#")
    return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))


# Sourced from hypogeum.brand.visual_identity (single canonical palette) —
# do not hardcode brand hex values here again.
_GOLD_COLOR  = _hex_to_rgb(visual_identity.get_palette()["aged_gold"]["hex"])       # antique_gold
_CREAM_COLOR = _hex_to_rgb(visual_identity.get_palette()["soft_off_white"]["hex"])  # parchment_cream

_FONT_SEARCH_PATHS = [
    # macOS system fonts
    "/System/Library/Fonts/Supplemental/Palatino.ttc",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    # Linux / CI
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_pil_font(size: int):
    """Return a PIL font at the requested size, falling back gracefully."""
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for path in _FONT_SEARCH_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    # Pillow ≥ 9.2 supports size argument on load_default()
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _render_typography_pil(
    output_dir: Path,
    deliverable_id: str,
    typography_plan: dict,
    log,
) -> Optional[Path]:
    """
    Render hook text as a PIL typography layer (black background, cream text).

    Replaces the former MUAPI typography layer call. Text is always deterministic:
    headline above emphasis, left-aligned in the lower-left zone, matching the
    brand palette. Saves to thumbnails/layers/{deliverable_id}_typography.jpg.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warn("compositor_pil_unavailable", deliverable_id=deliverable_id)
        return None

    W, H = 1280, 720
    img  = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    headline = (typography_plan.get("headline") or "").upper().strip()
    emphasis = (typography_plan.get("emphasis") or "").upper().strip()

    # Zone: lower-left, occupying x 2–42%, y 30–88%
    zone_x = int(W * 0.04)
    zone_w = int(W * 0.38)
    zone_y_start = int(H * 0.30)
    zone_y_end   = int(H * 0.88)

    # Font sizes: emphasis ~3× headline
    emphasis_size  = 88
    headline_size  = 36

    font_hl  = _load_pil_font(headline_size)
    font_em  = _load_pil_font(emphasis_size)

    y = zone_y_start

    if headline and font_hl:
        draw.text((zone_x, y), headline, font=font_hl, fill=_CREAM_COLOR)
        bbox = draw.textbbox((zone_x, y), headline, font=font_hl)
        y += (bbox[3] - bbox[1]) + 8

    if emphasis and font_em:
        # Word-wrap emphasis if too wide for zone
        words = emphasis.split()
        line  = ""
        for word in words:
            test = (line + " " + word).strip()
            bb   = draw.textbbox((zone_x, y), test, font=font_em)
            if bb[2] - bb[0] <= zone_w or not line:
                line = test
            else:
                draw.text((zone_x, y), line, font=font_em, fill=_GOLD_COLOR)
                bb2 = draw.textbbox((zone_x, y), line, font=font_em)
                y  += (bb2[3] - bb2[1]) + 4
                line = word
        if line:
            draw.text((zone_x, y), line, font=font_em, fill=_GOLD_COLOR)

    path = _layer_path(output_dir, deliverable_id, "typography")
    img.save(str(path), format="JPEG", quality=95)
    log.info("compositor_typography_pil_rendered", deliverable_id=deliverable_id,
             path=str(path))
    return path


def _apply_wordmark(composite_path: Path, log) -> None:
    """
    Overlay the brand wordmark at top-center of the finished composite.

    Three-line layout (matching brand visual identity):
      THE
      HYPOGEUM
      — CHRONICLES —
    Drawn in brand antique_gold (#C49A42) in the reserved top-12% zone.
    Modifies the file at composite_path in-place.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warn("compositor_wordmark_pil_unavailable")
        return

    img  = Image.open(composite_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Top zone: 0–12% height, centered horizontally
    zone_h = int(H * 0.12)
    line_spacing = 4

    font_sm  = _load_pil_font(13)
    font_med = _load_pil_font(18)
    font_sm2 = _load_pil_font(11)

    lines = [
        (_WORDMARK_LINE1, font_sm),
        (_WORDMARK_LINE2, font_med),
        (_WORDMARK_LINE3, font_sm2),
    ]

    # Measure total block height to vertically center in zone
    total_h = 0
    for text, font in lines:
        if font:
            bb = draw.textbbox((0, 0), text, font=font)
            total_h += (bb[3] - bb[1]) + line_spacing
    total_h = max(total_h, 1)

    y = max(4, (zone_h - total_h) // 2)

    for text, font in lines:
        if not font:
            continue
        bb   = draw.textbbox((0, 0), text, font=font)
        tw   = bb[2] - bb[0]
        x    = (W - tw) // 2
        draw.text((x, y), text, font=font, fill=_GOLD_COLOR)
        y   += (bb[3] - bb[1]) + line_spacing

    img.save(str(composite_path), format="JPEG", quality=95)
    log.info("compositor_wordmark_applied", path=str(composite_path))


# ---------------------------------------------------------------------------
# Layer generation
# ---------------------------------------------------------------------------

def _layer_path(output_dir: Path, deliverable_id: str, layer: str) -> Path:
    d = output_dir / "thumbnails" / LAYERS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{deliverable_id}_{layer}.jpg"


def _generate_layer(
    layer_name: str,
    prompt: str,
    deliverable_id: str,
    config: Config,
    output_dir: Path,
    log,
) -> Optional[Path]:
    """Generate one layer via MUAPI. Returns path on success, None on failure."""
    path = _layer_path(output_dir, deliverable_id, layer_name)
    log.info(
        "thumbnail_provider_attempt",
        provider="muapi",
        model=config.THUMBNAIL_IMAGE_MODEL,
        status="layered",
        layer=layer_name,
        deliverable_id=deliverable_id,
    )
    try:
        _generate_thumbnail_with_model(
            model=config.THUMBNAIL_IMAGE_MODEL,
            deliverable_id=f"{deliverable_id}_{layer_name}",
            image_prompt=prompt,
            aspect_ratio="16:9",
            config=config,
            local_path=path,
            budget_guard=None,
            log=log,
        )
        _normalize_thumbnail(path, "16:9")
        log.info("compositor_layer_generated", layer=layer_name, path=str(path))
        return path
    except Exception as exc:
        log.warn("compositor_layer_failed", layer=layer_name, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# PIL compositing
# ---------------------------------------------------------------------------

def _composite_layers(
    base_path: Path,
    subject_path: Optional[Path],
    fx_path: Optional[Path],
    typography_path: Optional[Path],
    output_path: Path,
) -> None:
    """
    Composite four layers into a finished thumbnail.

    Compositing strategy:
      base_scene   — foundation at full opacity (normal blend)
      fx           — screen blended over base (light adds, black = transparent)
      hero_subject — screen blended at right 55-60% of frame
      typography   — screen blended at left 40% of frame
      color grade  — contrast boost + warmth pass
      vignette     — radial darkening of corners
    """
    try:
        from PIL import Image, ImageChops, ImageEnhance, ImageDraw
    except ImportError:
        base_path.rename(output_path) if base_path != output_path else None
        return

    base = Image.open(base_path).convert("RGB")
    w, h = base.size

    def _screen(canvas: Image.Image, overlay: Image.Image) -> Image.Image:
        """Screen blend: overlay over canvas. Black areas of overlay are transparent."""
        o = overlay.convert("RGB").resize((w, h), Image.LANCZOS)
        return ImageChops.screen(canvas, o)

    def _place_and_screen(
        canvas: Image.Image,
        layer: Image.Image,
        x_frac: float, y_frac: float,
        w_frac: float, h_frac: float,
        feather_left: int = 0,
        feather_right: int = 0,
    ) -> Image.Image:
        """Place layer at fractional position, screen blend over canvas."""
        lw = round(w * w_frac)
        lh = round(h * h_frac)
        lx = round(w * x_frac)
        ly = round(h * y_frac)
        layer_r = layer.convert("RGB").resize((lw, lh), Image.LANCZOS)

        # Feather edges by blending into black on the soft side
        if feather_left > 0:
            layer_r = _feather_horizontal(layer_r, feather_left, side="left")
        if feather_right > 0:
            layer_r = _feather_horizontal(layer_r, feather_right, side="right")

        # Place on black canvas, then screen blend
        screen_canvas = Image.new("RGB", (w, h), (0, 0, 0))
        screen_canvas.paste(layer_r, (lx, ly))
        return ImageChops.screen(canvas, screen_canvas)

    def _feather_horizontal(img: Image.Image, pixels: int, side: str) -> Image.Image:
        """Fade the left or right edge of an image to black over `pixels` width."""
        iw, ih = img.size
        pixels = min(pixels, iw)
        arr = list(img.getdata())
        for py in range(ih):
            for px in range(pixels):
                alpha = px / pixels
                idx = py * iw + (px if side == "left" else (iw - 1 - px))
                r, g, b = arr[idx]
                arr[idx] = (int(r * alpha), int(g * alpha), int(b * alpha))
        img.putdata(arr)
        return img

    # ── Layer 1: base scene ───────────────────────────────────────────
    result = base

    # ── Layer 2: FX (atmospheric particles, screen) ───────────────────
    if fx_path and fx_path.exists():
        result = _screen(result, Image.open(fx_path))

    # ── Layer 3: Hero subject (right 55-60%, screen, left-edge feather) ──
    if subject_path and subject_path.exists():
        subj = Image.open(subject_path).convert("RGB")
        result = _place_and_screen(
            result, subj,
            x_frac=0.40, y_frac=0.00,
            w_frac=0.60, h_frac=1.00,
            feather_left=round(w * 0.06),   # soft blend into text zone
        )

    # ── Layer 4: Typography (left 40%, screen) ────────────────────────
    if typography_path and typography_path.exists():
        typo = Image.open(typography_path).convert("RGB")
        result = _place_and_screen(
            result, typo,
            x_frac=0.02, y_frac=0.10,
            w_frac=0.40, h_frac=0.80,
            feather_right=round(w * 0.04),  # soft blend into subject zone
        )

    # ── Color grade ───────────────────────────────────────────────────
    # Contrast
    result = ImageEnhance.Contrast(result).enhance(1.2)
    # Warmth: lift reds slightly, pull blue down
    r_ch, g_ch, b_ch = result.split()
    r_ch = r_ch.point(lambda p: min(255, int(p * 1.04) + 4))
    b_ch = b_ch.point(lambda p: max(0,   int(p * 0.90) - 4))
    result = Image.merge("RGB", (r_ch, g_ch, b_ch))

    # ── Vignette ──────────────────────────────────────────────────────
    result = _apply_vignette(result, strength=0.55)

    # ── Shadow floor clamp ────────────────────────────────────────────
    # Screen-blend compositing raises dark pixels; warm grade lifts them
    # further. This step crushes shadow/midtone pixels back toward
    # warm near-black, targeting ~65% pixels below brightness 25.
    result = _shadow_floor_clamp(result, gamma=1.8, warm_floor=(8, 5, 2))

    result.save(output_path, format="JPEG", quality=95)


def _shadow_floor_clamp(
    img: "Image.Image",
    gamma: float = 1.8,
    warm_floor: tuple = (8, 5, 2),
) -> "Image.Image":
    """
    Crush shadow and midtone pixels toward warm near-black.

    Applies a gamma > 1 curve using per-pixel luma as the scale driver:
        scale = (luma / 255) ^ (gamma - 1)

    This compresses luminance non-linearly — shadows are crushed hardest,
    highlights are barely touched:
        luma 50  → ~17  (near-black)
        luma 75  → ~28  (near-black)
        luma 100 → ~44  (dark shadow, 25-49 bucket)
        luma 150 → ~88  (midtone, preserved)
        luma 200 → ~165 (highlight, near-untouched)
        luma 255 → 255  (pure highlight, unchanged)

    R:G:B ratio is preserved per pixel (warm tint unchanged) because the
    scale factor is derived from luma and applied identically to all channels.
    warm_floor prevents any pixel from going pure-black; (8, 5, 2) retains a
    trace of warm amber in the deepest shadow zones.

    Uses numpy when available for vectorised speed. Falls back to a
    per-channel PIL LUT (slightly hue-shifting but fast and correct enough).

    Target: ~65% of output pixels below brightness 25.
    """
    from PIL import Image as _Image

    try:
        import numpy as np

        arr = np.array(img, dtype=np.float32)
        r_f = arr[:, :, 0]
        g_f = arr[:, :, 1]
        b_f = arr[:, :, 2]

        luma = 0.299 * r_f + 0.587 * g_f + 0.114 * b_f

        # Per-pixel scale: (luma/255)^(gamma-1); avoid div-by-zero at luma=0
        with np.errstate(divide="ignore", invalid="ignore"):
            scale = np.where(luma > 0, (luma / 255.0) ** (gamma - 1.0), 0.0)

        new_r = np.clip(r_f * scale, warm_floor[0], 255.0)
        new_g = np.clip(g_f * scale, warm_floor[1], 255.0)
        new_b = np.clip(b_f * scale, warm_floor[2], 255.0)

        out = np.stack([new_r, new_g, new_b], axis=2).astype(np.uint8)
        return _Image.fromarray(out, "RGB")

    except ImportError:
        # PIL-only fallback: per-channel LUT (luma-unaware, slight hue shift)
        exp = gamma
        factor = 255.0 ** (1.0 - (exp - 1.0))  # normalisation constant
        lut = [
            max(warm_floor[0], int((i ** exp) / (255 ** (exp - 1))))
            for i in range(256)
        ]
        r_ch, g_ch, b_ch = img.split()
        lut_g = [max(warm_floor[1], int((i ** exp) / (255 ** (exp - 1)))) for i in range(256)]
        lut_b = [max(warm_floor[2], int((i ** exp) / (255 ** (exp - 1)))) for i in range(256)]
        r_ch = r_ch.point(lut)
        g_ch = g_ch.point(lut_g)
        b_ch = b_ch.point(lut_b)
        return _Image.merge("RGB", (r_ch, g_ch, b_ch))


def _apply_vignette(img: "Image.Image", strength: float = 0.55) -> "Image.Image":
    """Radial vignette: darken corners, preserve center."""
    from PIL import Image, ImageDraw
    import math

    w, h = img.size
    # Build a radial gradient mask (white center, black edges)
    mask = Image.new("L", (w, h), 0)
    cx, cy = w / 2, h / 2
    max_r  = math.sqrt(cx ** 2 + cy ** 2)

    pixels = []
    for y in range(h):
        for x in range(w):
            r = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
            # Smooth cosine falloff from center
            t = min(1.0, r / max_r)
            bright = int(255 * (1.0 - strength * t ** 1.6))
            pixels.append(bright)
    mask.putdata(pixels)

    # Apply: multiply image by mask
    from PIL import ImageChops
    mask_rgb = Image.merge("RGB", (mask, mask, mask))
    return ImageChops.multiply(img, mask_rgb)


# ---------------------------------------------------------------------------
# APPROVED PRIMARY PATH — unified integrated-image generation
# (Thumbnail DNA v1, 2026-07-13)
# ---------------------------------------------------------------------------

def generate_unified_integrated_thumbnail(
    candidate: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    deliverable_id: str,
    log,
    apply_wordmark: bool = True,
) -> bool:
    """
    Approved primary thumbnail generation path. One unified GPT Image 2 call
    with integrated typography baked into the same prompt — no layered
    base/hero/FX calls, no PIL headline.

    Builds the final provider prompt deterministically from exactly four
    components, each present exactly once, in this fixed order:
        1. candidate["image_prompt"]  — topic-specific image concept, kept
           byte-identical as an exact prefix (never mutated/stripped)
        2. integrated headline typography instructions
           (thumbnail_prompt_writer._embed_typography_in_prompt), when
           candidate["typography_plan"] is present
        3. the compact Thumbnail DNA directive
           (thumbnail_composition_planner.describe_dna_constraints_compact),
           derived from candidate["composition_plan"]
        4. the compact FULL-tier brand visual-identity directive
           (hypogeum.brand.visual_identity.build_visual_identity_directive_compact,
           mode="thumbnail")

    2026-07-13 (V5.1 provider-safe prompt correction): switched components 3
    and 4 from the verbose/full directive builders to their compact
    counterparts after a real MUAPI "content could not be processed"
    rejection on a 6,730-character prompt. Every structured constraint is
    unchanged — describe_dna_constraints_compact() and
    build_visual_identity_directive_compact() read the exact same
    composition_plan fields / Brand Bible values as the full builders, only
    the rendered prose is shorter and each rule is stated once, not several
    times across components. The full/verbose builders remain unchanged and
    are still used by the layered path (build_layer_prompts()) and by
    hypogeum.agents.thumbnail_plan_validator / documentation.

    Single-source requirements: composition rules come only from
    thumbnail_composition_planner.py; brand palette/finish come only from
    hypogeum.brand.visual_identity (itself reading only
    docs/brand_guardian/brand_bible_hypogeum.json). No DNA or brand prose is
    duplicated or hardcoded here or anywhere episode-specific.

    One paid MUAPI call (config.THUMBNAIL_IMAGE_MODEL). No fallback, no
    retry after a valid image. Wordmark is applied via PIL afterward
    (TIG-07 requirement) unless apply_wordmark=False — no other PIL
    typography step runs; the headline is rendered by the image model.

    Args:
        candidate: ranked_candidate dict with image_prompt, and optionally
            typography_plan and composition_plan.
        config, episode_id, output_dir, deliverable_id, log: as elsewhere.
        apply_wordmark: True in production (TIG-07). Set False only for
            review/comparison generations that must not carry the wordmark.

    Returns:
        True on success (final image written), False on generation failure.
        Raises nothing — failures are logged and result in a False return.
    """
    # Local import: thumbnail_prompt_writer imports build_layer_prompts from
    # this module, so importing at module scope here would be circular.
    from hypogeum.agents.thumbnail_prompt_writer import _embed_typography_in_prompt

    base_prompt = candidate.get("image_prompt", "")
    typography_plan = candidate.get("typography_plan")
    composition_plan = candidate.get("composition_plan") or {}

    log.info(
        "unified_integrated_thumbnail_started",
        deliverable_id=deliverable_id,
        has_typography_plan=bool(typography_plan),
    )

    prompt = base_prompt
    if typography_plan:
        prompt = _embed_typography_in_prompt(prompt, typography_plan)
    prompt = prompt + describe_dna_constraints_compact(composition_plan)
    prompt = prompt + visual_identity.build_visual_identity_directive_compact(mode="thumbnail")
    visual_identity.log_palette_injection(log, mode="thumbnail", injected=True)

    out_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _generate_thumbnail_with_model(
            model=config.THUMBNAIL_IMAGE_MODEL,
            deliverable_id=deliverable_id,
            image_prompt=prompt,
            aspect_ratio="16:9",
            config=config,
            local_path=out_path,
            budget_guard=None,
            log=log,
        )
        _normalize_thumbnail(out_path, "16:9")
    except Exception as exc:  # noqa: BLE001
        log.error("unified_integrated_thumbnail_generation_failed",
                  deliverable_id=deliverable_id, error=str(exc)[:300])
        return False

    if apply_wordmark and out_path.exists():
        from hypogeum.muapi import _apply_wordmark_overlay
        _apply_wordmark_overlay(out_path)

    log.info("unified_integrated_thumbnail_finished", deliverable_id=deliverable_id,
             output=str(out_path),
             size_kb=round(out_path.stat().st_size / 1024) if out_path.exists() else 0)
    return out_path.exists()


# ---------------------------------------------------------------------------
# EXPERIMENTAL — hybrid unified-image path (not wired into production)
# ---------------------------------------------------------------------------

def generate_hybrid_thumbnail_experiment(
    unified_image_prompt: str,
    typography_plan: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    deliverable_id: str,
    log,
) -> bool:
    """
    EXPERIMENTAL hybrid thumbnail path — isolated from production.

    Generates ONE unified GPT Image 2 image containing both the environment
    and the hero subject in a single coherent camera/lighting/perspective
    decision, instead of the production path's separate base_scene +
    hero_subject layers. No FX layer in this experiment. Typography and
    wordmark are applied exactly as the production layered path does, reusing
    the same deterministic PIL steps.

    Does NOT call generate_and_composite(), _generate_layer(), or
    build_layer_prompts(). Does not read candidate["layer_prompts"]. The
    production multi_layer_composite path is completely untouched by this
    function and remains available for rollback/comparison.

    Args:
        unified_image_prompt: a single provider-ready prompt describing the
            full scene (environment + hero subject together) — not a
            concatenation of layer prompts.
        typography_plan:      same shape as the production typography_plan.
        config, episode_id, output_dir, deliverable_id, log: as elsewhere.

    Returns:
        True on success (final composite written), False if the unified
        image generation failed. Raises nothing — failures are logged and
        result in a False return, matching generate_and_composite()'s
        base_path-failure contract.
    """
    log.info("hybrid_experiment_started", deliverable_id=deliverable_id,
             mode="unified_single_image")

    branded_prompt = unified_image_prompt + visual_identity.build_visual_identity_directive(
        mode="thumbnail"
    )
    visual_identity.log_palette_injection(log, mode="thumbnail", injected=True)

    unified_path = _layer_path(output_dir, deliverable_id, "unified")
    try:
        _generate_thumbnail_with_model(
            model=config.THUMBNAIL_IMAGE_MODEL,
            deliverable_id=f"{deliverable_id}_unified",
            image_prompt=branded_prompt,
            aspect_ratio="16:9",
            config=config,
            local_path=unified_path,
            budget_guard=None,
            log=log,
        )
        _normalize_thumbnail(unified_path, "16:9")
    except Exception as exc:  # noqa: BLE001
        log.error("hybrid_experiment_unified_generation_failed",
                  deliverable_id=deliverable_id, error=str(exc)[:300])
        return False

    # PIL typography layer — deterministic, no MUAPI. Same function as production.
    typo_path = _render_typography_pil(output_dir, deliverable_id, typography_plan, log)

    # Composite: unified image as base, no hero_subject layer, no fx layer.
    out_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
    _composite_layers(unified_path, None, None, typo_path, out_path)

    # Wordmark overlay — mandatory brand identity step, same function as production.
    if out_path.exists():
        _apply_wordmark(out_path, log)

    log.info("hybrid_experiment_finished", deliverable_id=deliverable_id,
             output=str(out_path),
             size_kb=round(out_path.stat().st_size / 1024) if out_path.exists() else 0)
    return out_path.exists()


def finish_unified_thumbnail(
    unified_path: Path,
    typography_plan: dict,
    output_dir: Path,
    deliverable_id: str,
    log,
) -> bool:
    """
    EXPERIMENTAL — finish an ALREADY-GENERATED unified hybrid source image
    with typography + wordmark only. Zero provider calls. Does not
    regenerate, re-request, or re-tone the source in any way.

    Unlike _composite_layers() / generate_hybrid_thumbnail_experiment(),
    this function applies NO color grade (contrast/warmth push), NO
    vignette, and NO shadow-floor clamp. Those three steps exist in
    _composite_layers() specifically to compensate for screen-blend
    compositing raising dark pixels across multiple independently generated
    layers (base_scene + hero_subject + fx) — see
    docs/validation/thumbnail_image_generation_validation/
    COMPOSITE_PIPELINE_QUALITY_AUDIT.md. A single already-generated unified
    image never goes through that raising step, so applying the same
    correction over-crushes it. This function exists to skip that mismatch
    entirely rather than parameterize it.

    Pipeline: unified source (already normalized, unmodified) -> full-frame
    screen-blend of the PIL typography layer (rendered at full 1280x720,
    so no cropping/repositioning is needed) -> wordmark -> JPEG export.
    No hero blend, no fx blend, no color grade, no vignette, no shadow-floor
    clamp, no multi-layer tonal normalization.

    Args:
        unified_path: path to the already-generated, already-normalized
            unified source image (e.g. the *_unified.jpg file written by
            generate_hybrid_thumbnail_experiment()). Must already exist —
            this function never generates or fetches it.
        typography_plan, output_dir, deliverable_id, log: as elsewhere.

    Returns:
        True on success (final composite written), False if unified_path
        does not exist.
    """
    from PIL import Image, ImageChops

    if not unified_path.exists():
        log.error("finish_unified_thumbnail_source_missing", path=str(unified_path))
        return False

    base = Image.open(unified_path).convert("RGB")
    w, h = base.size

    # PIL typography layer — deterministic, no MUAPI. Same function as production.
    typo_path = _render_typography_pil(output_dir, deliverable_id, typography_plan, log)

    result = base
    if typo_path and typo_path.exists():
        # _render_typography_pil() always renders at the full 1280x720 frame
        # with text already placed at the correct zone — no resize/reposition
        # needed, unlike _composite_layers()'s sub-rectangle hero_subject
        # handling. Plain full-frame screen blend: black stays transparent.
        typo = Image.open(typo_path).convert("RGB")
        if typo.size != (w, h):
            typo = typo.resize((w, h), Image.LANCZOS)
        result = ImageChops.screen(result, typo)

    out_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(str(out_path), format="JPEG", quality=95)

    # Wordmark overlay — mandatory brand identity step, same function as production.
    if out_path.exists():
        _apply_wordmark(out_path, log)

    log.info("finish_unified_thumbnail_finished", deliverable_id=deliverable_id,
             output=str(out_path),
             size_kb=round(out_path.stat().st_size / 1024) if out_path.exists() else 0)
    return out_path.exists()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_and_composite(
    candidate: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    deliverable_id: str,
    log,
) -> bool:
    """
    Run the full multi-layer generation + compositing pipeline for one candidate.

    Visual layers (base_scene, hero_subject, fx) are generated via MUAPI.
    Typography layer is rendered deterministically by PIL (_render_typography_pil).
    Wordmark is applied as the mandatory final step by _apply_wordmark.

    Saves result to output_dir/thumbnails/{deliverable_id}.jpg.
    Returns True on success, False if base_scene generation failed.
    """
    layer_prompts = candidate.get("layer_prompts")
    if not layer_prompts:
        log.warn("compositor_no_layer_prompts", deliverable_id=deliverable_id)
        return False

    typography_plan = candidate.get("typography_plan") or {}

    log.info("compositor_started", deliverable_id=deliverable_id,
             layers=list(layer_prompts.keys()))

    # MUAPI visual layers (base_scene, hero_subject, fx)
    base_path = _generate_layer("base_scene",  layer_prompts["base_scene"],   deliverable_id, config, output_dir, log)
    subj_path = _generate_layer("hero_subject", layer_prompts["hero_subject"], deliverable_id, config, output_dir, log)
    fx_path   = _generate_layer("fx",           layer_prompts.get("fx", ""),   deliverable_id, config, output_dir, log)

    if base_path is None:
        log.error("compositor_base_failed", deliverable_id=deliverable_id)
        return False

    # PIL typography layer — deterministic, no MUAPI
    typo_path = _render_typography_pil(output_dir, deliverable_id, typography_plan, log)

    # Composite all layers
    out_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
    _composite_layers(base_path, subj_path, fx_path, typo_path, out_path)

    # Wordmark overlay — mandatory brand identity step
    if out_path.exists():
        _apply_wordmark(out_path, log)

    log.info("compositor_finished", deliverable_id=deliverable_id,
             output=str(out_path),
             size_kb=round(out_path.stat().st_size / 1024) if out_path.exists() else 0)
    return out_path.exists()


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_premium_thumbnail(output_dir: Path, deliverable_id: str) -> dict:
    """
    Validate that a premium (multi_layer_composite) thumbnail is complete.

    Checks for:
      - All four layer files under thumbnails/layers/
      - Final composite at thumbnails/{deliverable_id}.jpg

    Returns:
        {
          "valid": bool,
          "deliverable_id": str,
          "layers": {layer_name: {"path": str, "exists": bool, "size_kb": int}},
          "composite": {"path": str, "exists": bool, "size_kb": int},
          "missing": [str],   # names of missing items
        }
    """
    layer_dir = output_dir / "thumbnails" / LAYERS_SUBDIR
    composite  = output_dir / "thumbnails" / f"{deliverable_id}.jpg"

    layers_result: dict = {}
    missing: list[str] = []

    for name in LAYER_NAMES:
        p = layer_dir / f"{deliverable_id}_{name}.jpg"
        exists = p.exists()
        size_kb = round(p.stat().st_size / 1024) if exists else 0
        layers_result[name] = {"path": str(p), "exists": exists, "size_kb": size_kb}
        if not exists:
            missing.append(f"layer:{name}")

    comp_exists = composite.exists()
    comp_size   = round(composite.stat().st_size / 1024) if comp_exists else 0
    if not comp_exists:
        missing.append("composite")

    return {
        "valid":          len(missing) == 0,
        "deliverable_id": deliverable_id,
        "layers":         layers_result,
        "composite":      {"path": str(composite), "exists": comp_exists, "size_kb": comp_size},
        "missing":        missing,
    }
