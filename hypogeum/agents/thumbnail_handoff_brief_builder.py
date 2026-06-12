"""
hypogeum/agents/thumbnail_handoff_brief_builder.py — Thumbnail Handoff Brief builder (M2.4)

Extracted from visual_package_director.py.

Owns:
- Extracting thumbnail_handoff_brief from a finalized Visual Package
- Wrapping with envelope metadata (thumbnail_handoff_brief_id, episode_id, visual_package_id)
- Schema validation against 'thumbnail_handoff_brief'
- Writing thumbnail_handoff_brief_{episode_id}.json to disk
- Logging vpd_handoff_brief_written

Does NOT own:
- Visual Package assembly or MUAPI execution (visual_package_director owns this)
- Thumbnail planning or generation (thumbnail_prompt_writer / thumbnail_generation_agent)
- Schema validation of the Visual Package itself (visual_package_director owns this)

MUAPI attribution: n/a — this agent makes no MUAPI calls.
Logger attribution: agent = "visual_package_director" (preserved for audit log continuity).

Public API:
    build_thumbnail_handoff_brief(
        visual_package, episode_id, output_dir, log=None,
    ) -> dict
    Returns: the standalone Thumbnail Handoff Brief dict (schema-validated).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger
from hypogeum.schemas import validate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "visual_package_director"  # preserved for audit log continuity


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_thumbnail_handoff_brief(
    visual_package: dict,
    episode_id: str,
    output_dir: Path,
    log=None,
) -> dict:
    """
    Extract the Thumbnail Handoff Brief from the finalized Visual Package.

    Wraps the embedded thumbnail_handoff_brief with envelope metadata (IDs),
    validates it against the standalone 'thumbnail_handoff_brief' schema, and
    writes it to thumbnail_handoff_brief_{episode_id}.json.

    This file is the Thumbnail Director's sole input. TD never reads the full
    Visual Package — the clean boundary is enforced here.

    Args:
        visual_package: Finalized Visual Package dict (post-MUAPI execution).
        episode_id:     Episode identifier for file naming.
        output_dir:     Directory where the brief file is written.
        log:            Logger instance. If None, one is created.

    Returns:
        The standalone Thumbnail Handoff Brief dict (schema-validated).

    Raises:
        SchemaError — Brief extracted from Visual Package fails validation.
                      Callers may choose to continue (TD cannot run without brief).
    """
    if log is None:
        log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    embedded = visual_package.get("thumbnail_handoff_brief", {})
    meta = visual_package.get("package_metadata", {})

    brief = {
        "thumbnail_handoff_brief_id": f"thb_{episode_id}",
        "episode_id": episode_id,
        "visual_package_id": meta.get("visual_package_id", f"vp_{episode_id}"),
        "central_curiosity_gap_statement": embedded.get(
            "central_curiosity_gap_statement", ""
        ),
        "candidates": embedded.get("candidates", []),
        "short_candidates": embedded.get("short_candidates", []),
        "primary_subject_identity": embedded.get("primary_subject_identity", {}),
        "episode_color_palette": embedded.get("episode_color_palette", ""),
        "negative_constraints": embedded.get("negative_constraints", []),
    }

    validate("thumbnail_handoff_brief", brief)

    out_path = output_dir / f"thumbnail_handoff_brief_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(brief, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "vpd_handoff_brief_written",
        output_path=str(out_path),
        candidates_count=len(brief["candidates"]),
        short_candidates_count=len(brief["short_candidates"]),
    )

    return brief
