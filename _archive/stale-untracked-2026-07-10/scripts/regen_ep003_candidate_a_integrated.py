"""
scripts/regen_ep003_candidate_a_integrated.py

Regenerate ep_003 candidate A (IT SHOULDN'T EXIST) using the reference image
visual grammar: complete designed thumbnail, integrated typography, no PIL text overlay.

Bypasses Claude planning entirely — uses the reference-derived prompt directly.
Calls MUAPI once for candidate_1 only. Applies wordmark via PIL only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.config import Config
from hypogeum.muapi import generate_thumbnail
from hypogeum.logger import get_logger

EPISODE_ID  = "ep_003"
ASSETS_ROOT = Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "episodes" / EPISODE_ID

# ---------------------------------------------------------------------------
# Reference-derived prompt — describes the complete thumbnail as one design
# ---------------------------------------------------------------------------

CANDIDATE_A_PROMPT = """
A dramatic cinematic YouTube thumbnail for a historical mystery documentary.
This is a complete designed thumbnail — text, subject, and environment are \
designed together as one unified composition, not a background with text added after.

COMPOSITION:
Left 40% of frame: dominated by huge integrated typography.
Right 60% of frame: dominated by the ancient mechanism and deep environment.
Strong foreground / midground / background layering throughout.

LEFT SIDE — TYPOGRAPHY (physically part of the image, not an overlay):
In the left 40% of the frame, large distressed gold-warm text reads exactly:
Line 1: "IT SHOULDN'T" in smaller lighter-weight letters
Line 2: "EXIST" in enormous dominant letters directly below — dramatically larger \
than the top line, filling most of the lower-left quadrant.
The text is NOT a subtitle. NOT an overlay. NOT in a box. NOT a lower-third.
The text has realistic cracked stone / distressed gold texture, warm amber torchlight \
catching the letter faces, realistic drop shadows, strong contrast against the dark background.
The text is the first thing the eye reads at thumbnail scale.
The letters feel physically carved or cast into the scene — they have weight, depth, and age.

RIGHT SIDE — SUBJECT:
On the right 55–60% of the frame: a huge ancient bronze astronomical mechanism \
embedded in broken stone. Massive visible interlocking gears, circular dials with \
engraved rings, corroded bronze with green patina, intricate mechanical precision. \
The mechanism is sharp, detailed, and fully readable at thumbnail size. \
It fills the entire right side of the frame from near-top to near-bottom.

ENVIRONMENT (layered depth throughout):
Dark underground stone workshop or ancient hypogeum chamber. \
Rough irregular stone walls with faint astronomical diagrams and carved symbols \
in the background (not readable as text, just atmospheric texture). \
Ancient iron tools and wooden implements scattered in the foreground. \
Shelves with clay pots and shadowed artifacts in the far background. \
Warm amber torchlight from upper-left. Subtle cool shadow tones for depth. \
Volumetric dust and atmospheric haze. Deep black corners.

LIGHTING:
Cinematic chiaroscuro. Warm amber torch key light from upper-left. \
Strong rim light defining the right edge of the mechanism. \
Realistic warm shadow on the left-side typography. High contrast. \
Warm amber and deep brown palette. Dramatic depth.

ZONES (hard requirements):
Top 12% of frame: near-black or very deep shadow — completely clear of mechanism, \
figures, text, and any inscriptions — reserved for brand wordmark.
Bottom 10% of frame: completely empty — no captions, no model-generated labels, \
no inscriptions, no readable text of any kind.
No random model-generated text or inscriptions anywhere except the exact two lines specified above.

STYLE:
Premium cinematic YouTube mystery thumbnail. CTR-first. High-end. Layered. Sharp. \
Readable at 320px wide. Not documentary poster art. Not museum product shot. \
No flat black void. No text backing rectangle. No subtitle band. No lower-third.
""".strip()


def main() -> int:
    config = Config()
    log    = get_logger(episode_id=EPISODE_ID, agent="integrated_typography")

    print(f"\n{'='*62}")
    print("  ep_003 Candidate A — Integrated Typography Mode")
    print(f"{'='*62}")
    print(f"  model: {config.THUMBNAIL_IMAGE_MODEL}")
    print(f"  prompt length: {len(CANDIDATE_A_PROMPT)} chars")
    print()

    result = generate_thumbnail(
        deliverable_id="candidate_1",
        image_prompt=CANDIDATE_A_PROMPT,
        aspect_ratio="16:9",
        config=config,
        episode_id=EPISODE_ID,
        output_dir=ASSETS_ROOT,
        agent="integrated_typography",
        typography_integration="embedded",   # PIL text compositor disabled
    )

    path = ASSETS_ROOT / "thumbnails" / "candidate_1.jpg"
    size_kb = round(path.stat().st_size / 1024) if path.exists() else 0

    print(f"  Path:   {path}")
    print(f"  Size:   {size_kb} KB {'✓' if path.exists() else '✗'}")
    print(f"  Asset:  {result.asset_id}")
    print(f"  Model:  {result.model_used}")
    print(f"{'='*62}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
