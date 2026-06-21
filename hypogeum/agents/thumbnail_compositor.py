"""
hypogeum/agents/thumbnail_compositor.py — Multi-layer thumbnail compositor

Thumbnail mode registry
-----------------------
THUMBNAIL_MODE_PREMIUM  = "multi_layer_composite"
    Main path for all premium main-episode thumbnails.
    Generates 4 independent layers via MUAPI, composites with PIL screen-blend.
    Required layers: base_scene, hero_subject, fx, typography.
    Use generate_and_composite() with candidate["composite_mode"] = True.

THUMBNAIL_MODE_FAST     = "single_pass_integrated"
    Fast/low-cost mode. Single MUAPI call with embedded typography instruction.
    Used for rapid iteration, cost-capped runs, or shorts.
    Falls through the _generate_for_deliverable() path in thumbnail_generation_agent.

THUMBNAIL_MODE_LEGACY   = "pil_composited_text"
    Fallback only. Single image generation + PIL text overlay (old behaviour).
    Only reached when typography_plan is present but composite_mode is False.

Layered compositing workflow (PREMIUM only):
  Layer 1 — base_scene:    Environment plate only (no subject, no text)
  Layer 2 — hero_subject:  Isolated subject on dark background
  Layer 3 — fx:            Atmospheric effects on black (screen blended)
  Layer 4 — typography:    Distressed gold text on black (screen blended)
  → PIL composite → color grade → vignette

Public API:
    build_layer_prompts(concept, composition_plan) -> dict
    generate_and_composite(candidate, config, episode_id, output_dir, deliverable_id, log) -> bool
    validate_premium_thumbnail(output_dir, deliverable_id) -> dict
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.muapi import _generate_thumbnail_with_model, _normalize_thumbnail


# ---------------------------------------------------------------------------
# Thumbnail mode constants
# ---------------------------------------------------------------------------

THUMBNAIL_MODE_PREMIUM = "multi_layer_composite"    # main path
THUMBNAIL_MODE_FAST    = "single_pass_integrated"   # fast/cost mode
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
    Build four focused layer prompts from a concept dict.

    Each prompt is optimised for its single job:
      base_scene   — environment only, no subject, no text
      hero_subject — subject only on dark background (screen blend ready)
      fx           — atmospheric particles/light on black (screen blend)
      typography   — large distressed gold text on black (screen blend)

    Args:
        concept:          One concept dict from thumbnail_concept_brief.concepts[].
        composition_plan: composition_plan from plan_composition().

    Returns:
        dict with keys: base_scene, hero_subject, fx, typography
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

    # Typography layer — text on black for screen compositing
    if headline:
        typo_layout = (
            f'Top line: "{headline}" in smaller, lighter-weight letters.\n'
            f'Bottom line: "{emphasis}" in enormous dominant letters, '
            f'at least 3× taller than the top line, filling most of the frame height.'
        )
    else:
        typo_layout = (
            f'Single line: "{emphasis}" in enormous dominant letters filling most of the frame.'
        )

    typography_prompt = (
        f"Pure solid black background. "
        f"Large distressed stone-and-gold typography centered in frame.\n"
        f"{typo_layout}\n"
        f"Letter material: ancient cracked stone surface with warm parchment-gold highlights. "
        f"Dark grime visible inside letter cracks. Subtle bevel depth and shadow. "
        f"Weathered, aged, textured. The text catches warm amber light. "
        f"Realistic shadows beneath letters. Strong contrast. "
        f"NO other visual elements. NO background texture. Pure black except the letters. "
        f"Text must be fully legible and correctly spelled."
    )

    return {
        "base_scene":   env_prompt,
        "hero_subject": subject_prompt,
        "fx":           fx_prompt,
        "typography":   typography_prompt,
    }


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

    Reads candidate["layer_prompts"] (set by thumbnail_prompt_writer._attach_concept_plans).
    Generates each layer via MUAPI.
    Composites with PIL.
    Saves result to output_dir/thumbnails/{deliverable_id}.jpg.

    Returns True on success, False if base_scene generation failed.
    """
    layer_prompts = candidate.get("layer_prompts")
    if not layer_prompts:
        log.warn("compositor_no_layer_prompts", deliverable_id=deliverable_id)
        return False

    log.info("compositor_started", deliverable_id=deliverable_id,
             layers=list(layer_prompts.keys()))

    # Generate each layer
    base_path  = _generate_layer("base_scene",   layer_prompts["base_scene"],   deliverable_id, config, output_dir, log)
    subj_path  = _generate_layer("hero_subject",  layer_prompts["hero_subject"], deliverable_id, config, output_dir, log)
    fx_path    = _generate_layer("fx",            layer_prompts.get("fx", ""),   deliverable_id, config, output_dir, log)
    typo_path  = _generate_layer("typography",    layer_prompts["typography"],   deliverable_id, config, output_dir, log)

    if base_path is None:
        log.error("compositor_base_failed", deliverable_id=deliverable_id)
        return False

    # Composite
    out_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
    _composite_layers(base_path, subj_path, fx_path, typo_path, out_path)

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
