"""
hypogeum/agents/thumbnail_compositor.py — Multi-layer thumbnail compositor

Evolves the thumbnail pipeline from single-pass image generation to a
layered compositing workflow:

  Layer 1 — base_scene:    Environment plate only (no subject, no text)
  Layer 2 — hero_subject:  Isolated subject on dark background
  Layer 3 — fx:            Atmospheric effects on black (screen blended)
  Layer 4 — typography:    Distressed gold text on black (screen blended)
  → PIL composite → color grade → vignette → wordmark

This produces thumbnail-design quality instead of scene-generation quality
because each layer is optimised for its single job and compositing with
screen blend mode gives natural integration.

Public API:
    build_layer_prompts(concept, composition_plan) -> dict
    generate_and_composite(candidate, config, episode_id, output_dir, log) -> Path
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.muapi import _generate_thumbnail_with_model, _normalize_thumbnail


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

    result.save(output_path, format="JPEG", quality=95)


def _apply_vignette(img: Image.Image, strength: float = 0.55) -> Image.Image:
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
