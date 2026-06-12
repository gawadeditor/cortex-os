"""
hypogeum/agents/music_agent.py — Music Agent

Sole authority for all music sourcing. Sources background music tracks for
every section of an episode. Assembly Agent must not call generate_music_track
or any ElevenLabs music endpoint directly — it receives a music_track_package.

Inputs:
  visual_package     — episode_metadata.tone_arc drives category selection
  episode_id         — for asset path construction and log correlation
  config             — Config instance (ElevenLabs credentials)
  assets_root        — root directory for music library and asset storage
  section_durations  — {section_key: required_duration_seconds} from Assembly Agent
                       Keys: "main_episode", "short_{short_id}", ...

Output (music_track_package):
  tracks              — {section_key: track_dict} for sections with music
  music_log           — per-section sourcing history (for final_render_package)
  sections_without_music — section keys that got no track
  sourcing_status     — "complete" / "partial" / "failed"

Sourcing chain per section:
  1. Channel Music Library lookup (library-first; free)
  2. ElevenLabs AI generation fallback (on cache miss)
  3. Section recorded in sections_without_music on exception

Three-consecutive-failure safety: ≥3 consecutive failures marks the provider
as unavailable; remaining sections are skipped without API calls.

No Claude calls. No LLM.

Public API:
    run_music_agent(
        visual_package, episode_id, config, assets_root, section_durations
    ) → dict  (music_track_package, schema-validated)

Logging events:
  music_agent_started        — info  — episode_id, section count
  music_sourcing_start       — info  — per-section before generate_music_track
  music_sourcing_success     — info  — section sourced, source type
  music_sourcing_failed      — error — exception detail, consecutive_failures count
  music_provider_unavailable — warn  — ≥3 failures; remaining sections skipped
  music_agent_completed      — info  — final status summary
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.elevenlabs import (
    ElevenLabsError,
    MusicTrackResult,
    generate_music_track,
)
from hypogeum.logger import get_logger
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "music_agent"
_AGENT_VERSION = "1.0"

# Consecutive-failure threshold before marking provider unavailable
_PROVIDER_UNAVAILABLE_THRESHOLD = 3

# Minimum section durations (seconds) — prevent very short requests
_MIN_MAIN_DURATION = 30.0
_MIN_SHORT_DURATION = 10.0

# Valid music categories (§8 frozen spec)
_MUSIC_CATEGORIES = frozenset({
    "Mystery", "Discovery", "Investigation",
    "Ancient Civilizations", "Tension", "Resolution",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_music_agent(
    visual_package: dict,
    episode_id: str,
    config: Config,
    assets_root: Path,
    section_durations: dict[str, float],
) -> dict:
    """
    Source background music tracks for all sections in section_durations.

    Args:
        visual_package:    Visual Package dict (episode_metadata.tone_arc used
                           for category selection).
        episode_id:        Episode identifier for asset path construction.
        config:            Loaded Config instance.
        assets_root:       Root of .hypogeum-assets/ tree.
        section_durations: {section_key: required_duration_seconds} mapping.
                           Keys: "main_episode", "short_{short_id}", ...
                           From Assembly Agent's timeline computation.

    Returns:
        music_track_package dict (schema-validated).

    Raises:
        Nothing — all ElevenLabs failures are caught and recorded as
        sections_without_music. The package is always returned.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    log.info(
        "music_agent_started",
        episode_id=episode_id,
        section_count=len(section_durations),
        sections=sorted(section_durations.keys()),
    )

    package_id = f"mtp_{episode_id}_{uuid.uuid4().hex[:8]}"
    sourced_at = datetime.now(timezone.utc).isoformat()

    # Derive tone_arc and category mapping
    ep_meta = visual_package.get("episode_metadata", {})
    tone_arc = ep_meta.get("tone_arc", "")
    section_categories = _build_section_categories(
        section_keys=list(section_durations.keys()),
        tone_arc=tone_arc,
    )

    tracks: dict[str, dict] = {}
    music_log: list[dict] = []
    sections_without_music: list[str] = []

    consecutive_failures = 0
    provider_unavailable = False

    for section_key, required_duration in section_durations.items():
        category = section_categories.get(section_key, "Mystery")

        # --- Provider unavailable shortcut ---
        if provider_unavailable:
            log.warn(
                "music_provider_unavailable",
                episode_id=episode_id,
                section=section_key,
            )
            sections_without_music.append(section_key)
            music_log.append(_music_log_entry(
                log_id=f"ml_{section_key}_unavailable",
                placed_in=section_key,
                category=category,
                source=None,
                asset_id=None,
                fallback_trigger="provider_unavailable",
            ))
            continue

        log.info(
            "music_sourcing_start",
            episode_id=episode_id,
            section=section_key,
            category=category,
            required_duration=required_duration,
        )

        try:
            track: MusicTrackResult = generate_music_track(
                category=category,
                required_duration_seconds=required_duration,
                config=config,
                episode_id=episode_id,
                assets_root=assets_root,
                tone_arc=tone_arc,
                agent=_AGENT_NAME,
            )
            # Success — record track
            tracks[section_key] = {
                "section_key": section_key,
                "category": category,
                "asset_id": track.asset_id,
                "local_path": str(track.local_path),
                "source": track.source,
                "duration_seconds": track.duration_seconds,
                "prompt_used": track.prompt_used or "",
            }
            consecutive_failures = 0
            music_log.append(_music_log_entry(
                log_id=f"ml_{section_key}",
                placed_in=section_key,
                category=category,
                source=track.source,
                asset_id=track.asset_id,
                fallback_trigger=None,
            ))
            log.info(
                "music_sourcing_success",
                episode_id=episode_id,
                section=section_key,
                source=track.source,
                asset_id=track.asset_id,
            )

        except Exception as exc:
            consecutive_failures += 1
            sections_without_music.append(section_key)
            music_log.append(_music_log_entry(
                log_id=f"ml_{section_key}_failed",
                placed_in=section_key,
                category=category,
                source=None,
                asset_id=None,
                fallback_trigger="provider_failure",
                confidence_note=str(exc)[:200],
            ))
            log.error(
                "music_sourcing_failed",
                episode_id=episode_id,
                section=section_key,
                category=category,
                error=str(exc)[:200],
                consecutive_failures=consecutive_failures,
            )
            if consecutive_failures >= _PROVIDER_UNAVAILABLE_THRESHOLD:
                provider_unavailable = True
                log.warn(
                    "music_provider_unavailable",
                    episode_id=episode_id,
                    reason=f"{_PROVIDER_UNAVAILABLE_THRESHOLD} consecutive failures",
                )

    # Determine sourcing status
    total = len(section_durations)
    sourced = len(tracks)
    if sourced == 0 and total > 0:
        sourcing_status = "failed"
    elif sourced < total:
        sourcing_status = "partial"
    else:
        sourcing_status = "complete"

    log.info(
        "music_agent_completed",
        episode_id=episode_id,
        sections_total=total,
        sections_sourced=sourced,
        sections_without_music=sections_without_music,
        sourcing_status=sourcing_status,
    )

    package = {
        "package_id": package_id,
        "episode_id": episode_id,
        "sourced_at": sourced_at,
        "sourcing_status": sourcing_status,
        "tracks": tracks,
        "music_log": music_log,
        "sections_without_music": sections_without_music,
    }

    validate("music_track_package", package)
    return package


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_section_categories(
    section_keys: list[str],
    tone_arc: str,
) -> dict[str, str]:
    """Map each section key to a music category derived from the episode tone arc."""
    base_category = _tone_arc_to_category(tone_arc)
    return {key: base_category for key in section_keys}


def _tone_arc_to_category(tone_arc: str) -> str:
    """Map episode tone arc description to one of the six music categories."""
    tone_lower = (tone_arc or "").lower()
    if "ancient" in tone_lower or "civilization" in tone_lower:
        return "Ancient Civilizations"
    if "mystery" in tone_lower or "unknown" in tone_lower:
        return "Mystery"
    if "discover" in tone_lower or "explor" in tone_lower:
        return "Discovery"
    if "investigat" in tone_lower:
        return "Investigation"
    if "tension" in tone_lower or "urgent" in tone_lower:
        return "Tension"
    if "resolv" in tone_lower or "conclusion" in tone_lower:
        return "Resolution"
    return "Mystery"


def _music_log_entry(
    log_id: str,
    placed_in: str,
    category: str,
    source: Optional[str],
    asset_id: Optional[str],
    fallback_trigger: Optional[str],
    confidence_note: Optional[str] = None,
) -> dict:
    """Build one music_log[] entry compatible with final_render_package.music_log."""
    return {
        "log_id": log_id,
        "placed_in": placed_in,
        "timeline_position_seconds": 0.0,
        "category": category,
        "music_asset_id": asset_id or "",
        "source": source or "ai_generated",
        "provider": "elevenlabs" if source == "ai_generated" else None,
        "confidence_note": confidence_note,
        "fallback_trigger": fallback_trigger,
    }
