"""
hypogeum/publishing/deliverable_ids.py — Canonical deliverable_id
resolution, shared by hypogeum.buffer and
hypogeum.publishing.zernio_channel_release_trigger, so "which
deliverable_ids make up one item's bundle" is computed exactly once —
never reimplemented twice with a risk of disagreeing (the earlier version
of this logic lived only in zernio_channel_release_trigger.py; buffer.py
needs the identical answer to correctly determine bundle completeness —
see hypogeum/buffer.py's own already_published logic).

Convention: 'main_episode' for the long-form; 'short_<short_id>' for each
Short — matching hypogeum.agents.publishing_agent._deliverable_id_for()'s
own, already-established, channel-agnostic convention exactly (never a
new scheme invented here). See resolve_short_deliverable_id()'s own
docstring for the one narrow, generic fallback applied when a render
package's own short_id is missing or still a generation-time placeholder.

Read-only throughout. No database or network access — only reads
final_render_package_{item_id}.json from disk.

Public API:
    MAIN_EPISODE_DELIVERABLE_ID = "main_episode"
    resolve_short_deliverable_id(short_entry, index) -> str
    resolve_required_deliverable_ids(item_id, assets_root) -> list[str]
    classify_deliverable_class(deliverable_id) -> str  # 'main_episode' | 'shorts'
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

MAIN_EPISODE_DELIVERABLE_ID = "main_episode"


def resolve_short_deliverable_id(short_entry: Optional[dict], index: int) -> str:
    """
    Canonical deliverable_id for one Short, matching the SAME convention
    hypogeum.agents.publishing_agent._deliverable_id_for('short', short_id)
    already establishes (f"short_{short_id}") for the legacy direct
    adapter — using the render package's own shorts_renders[i].short_id
    when it is a real value.

    Falls back to a clean, index-derived id (short_{i+1:03d}, 1-indexed,
    zero-padded) when the render package's own short_id is missing or
    still a generation-time placeholder (contains '_placeholder') — the
    exact real-world case confirmed for ep_f3619022: shorts_renders[].
    short_id was never corrected on disk, but the actual historical
    publish used 'short_001'/'short_002'/'short_003' (verified against the
    sealed publishing_record_ep_f3619022.json and publish_calendar.
    short_id — the only place the corrected values survive). This is a
    generic "clean up a broken/placeholder short_id" rule, applicable to
    any channel's episode with the same defect — nothing here is
    Hypogeum-specific.
    """
    raw_short_id = (short_entry or {}).get("short_id") or ""
    if raw_short_id and "_placeholder" not in raw_short_id:
        return f"short_{raw_short_id}"
    return f"short_{index + 1:03d}"


def classify_deliverable_class(deliverable_id: str) -> str:
    """
    Generic, channel-agnostic deliverable CLASS ('main_episode' or
    'shorts') derived purely from the canonical deliverable_id convention
    this module itself owns -- MAIN_EPISODE_DELIVERABLE_ID is the one
    long-form id; every other deliverable_id (any 'short_<...>' value
    resolve_short_deliverable_id() ever produces) is a Short. Used by
    Channel Package platform-distribution policy
    (ChannelOperationsProfile.platform_deliverable_policy) to apply one
    rule to "the main episode" and a separate rule to "every Short"
    without the policy needing to enumerate individual short_ids.
    """
    return MAIN_EPISODE_DELIVERABLE_ID if deliverable_id == MAIN_EPISODE_DELIVERABLE_ID else "shorts"


def resolve_required_deliverable_ids(item_id: str, assets_root: Path) -> List[str]:
    """
    Read-only. Returns the full ['main_episode', 'short_<...>', ...] list
    for one item, derived from its own final_render_package.json
    shorts_renders[] — the same source
    hypogeum.publishing.zernio_channel_release_trigger.
    build_publish_package_from_episode_dir() reads to build the actual
    files to stage.

    An item with no final_render_package on disk yet is treated as
    requiring ONLY 'main_episode' — the smallest possible, never-invented
    requirement (shorts count is genuinely unknown, not assumed absent).
    """
    episode_dir = Path(assets_root) / "episodes" / item_id
    render_path = episode_dir / f"final_render_package_{item_id}.json"
    final_render_package: dict = {}
    if render_path.exists():
        try:
            final_render_package = json.loads(render_path.read_text(encoding="utf-8"))
        except Exception:
            final_render_package = {}
    shorts_renders = (final_render_package or {}).get("shorts_renders", []) or []
    return [MAIN_EPISODE_DELIVERABLE_ID] + [
        resolve_short_deliverable_id(shorts_renders[i], i) for i in range(len(shorts_renders))
    ]
