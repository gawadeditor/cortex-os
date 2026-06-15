#!/usr/bin/env python3
"""
scripts/bootstrap_channel.py — P1-01: Bootstrap Hypogeum channel into live DB.

Populates brand_profiles, publishing_profiles, model_profiles, and
channel_profiles with the Hypogeum Chronicles configuration. Safe to
run repeatedly — bootstrap_channel() is idempotent on channel_id.

Model slugs are read from .env (via Config) so the DB record always
reflects the currently active model assignments.

Usage:
    python3 scripts/bootstrap_channel.py
    python3 scripts/bootstrap_channel.py --verify-only

Exit codes:
    0 — channel bootstrapped (or already existed) and verified
    1 — bootstrap failed or verification failed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.channel_config import (
    bootstrap_channel,
    get_channel_profile,
    get_brand_profile,
    get_publishing_profile,
    get_model_profile,
    load_channel_config,
    list_channel_profiles,
)
from hypogeum.config import Config
from hypogeum.dashboard_api import get_notifications

# ---------------------------------------------------------------------------
# Hypogeum channel ID (stable slug — never change after bootstrap)
# ---------------------------------------------------------------------------

CHANNEL_ID = "hypogeum"


def _build_config_dict(config: Config) -> dict:
    """
    Construct the bootstrap config dict from .env values.

    Model slugs read from Config so the DB record always matches the
    current environment. Brand and publishing values are canonical
    Hypogeum identity — not environment-driven.
    """
    return {
        "channel_id": CHANNEL_ID,
        "name": "The Hypogeum Chronicles",
        "description": (
            "Ancient mysteries, hidden histories, and archaeological revelations. "
            "Investigation-led documentary content for history and archaeology enthusiasts."
        ),
        "brand": {
            "persona_name": "The Hypogeum Chronicles",
            "tone": "investigative, evidence-based, mystery-led",
            "target_audience": "history and archaeology enthusiasts, 25–55",
            "visual_style": (
                "warm earth tones, underground and ancient architecture, "
                "atmospheric darkness, archaeological sites and artefacts"
            ),
            "language": "en",
            "hashtags": [
                "#history", "#archaeology", "#ancienthistory",
                "#mystery", "#hiddenhistory", "#documentary",
            ],
            "color_palette": {
                "void_black":       "#080C0E",
                "deep_stone":       "#1A1D20",
                "charcoal_earth":   "#2B2E33",
                "ancient_soil":     "#5C4A2F",
                "antique_gold":     "#C49A42",
                "warm_gold":        "#E7D6B3",
                "parchment_cream":  "#F5F3EE",
            },
            "typography": {
                "display_font":    "Cinzel SemiBold",
                "body_font":       "Raleway Regular",
                "display_case":    "ALL CAPS",
                "body_case":       "sentence case",
            },
            "voice_guidelines": (
                "Narrator: authoritative yet accessible. Investigation-led. "
                "No sensationalism. Evidence presented before conclusions. "
                "Pacing: measured — allow revelations to land. "
                "Tone: scholarly curiosity, not entertainment spectacle."
            ),
            "content_pillars": [
                "ancient history",
                "archaeology",
                "historical mysteries",
                "hidden histories",
                "artefact investigation",
                "civilisation origins",
            ],
        },
        "publishing": {
            "enabled_platforms": ["youtube"],
            "schedule": {
                "cadence":     "weekly",
                "day_of_week": "thursday",
                "time_of_day": "18:00",
                "timezone":    "UTC",
            },
            "default_tags": [
                "history", "archaeology", "ancient history",
                "historical mysteries", "documentary", "hidden history",
            ],
            "ai_disclosure_required": True,
            "auto_thumbnail":          True,
            "auto_captions":           True,
        },
        "models": {
            "episode_producer":   config.EP_MODEL,
            "research_director":  config.RD_MODEL,
            "thumbnail_planner":  config.TD_PLANNING_MODEL,
            "brand_guardian_p1":  config.BGA_PASS1_MODEL,
            "brand_guardian_p2":  config.BGA_PASS2_MODEL,
            "narration_voice_id": config.ELEVENLABS_VOICE_ID,
            "image_provider":     "muapi",
            "video_provider":     "muapi",
        },
    }


def _run_bootstrap(config: Config) -> bool:
    """Bootstrap the channel. Returns True on success."""
    config_dict = _build_config_dict(config)

    print(f"Bootstrapping channel: {CHANNEL_ID!r}")

    # Check for existing channel first to give a clear message
    existing = get_channel_profile(CHANNEL_ID)
    if existing:
        print(f"  Channel already exists (status={existing.status!r}) — skipping insert.")
        return True

    channel = bootstrap_channel(config_dict)
    print(f"  Created channel_id={channel.channel_id!r}")
    print(f"  brand_profile_id={channel.brand_profile_id!r}")
    print(f"  publishing_profile_id={channel.publishing_profile_id!r}")
    print(f"  model_profile_id={channel.model_profile_id!r}")
    print(f"  status={channel.status!r}")
    return True


def _run_verification() -> bool:
    """Verify all four tables have rows and channel is accessible. Returns True on success."""
    print("\nVerification:")

    ok = True

    # 1. channel_profiles
    channel = get_channel_profile(CHANNEL_ID)
    if channel is None:
        print(f"  FAIL  channel_profiles — no row for channel_id={CHANNEL_ID!r}")
        return False
    print(f"  PASS  channel_profiles — channel_id={channel.channel_id!r}  status={channel.status!r}")

    # 2. brand_profiles
    brand = get_brand_profile(channel.brand_profile_id) if channel.brand_profile_id else None
    if brand is None:
        print(f"  FAIL  brand_profiles — no row for brand_profile_id={channel.brand_profile_id!r}")
        ok = False
    else:
        print(f"  PASS  brand_profiles  — persona={brand.persona_name!r}  language={brand.language!r}")

    # 3. publishing_profiles
    pub = get_publishing_profile(channel.publishing_profile_id) if channel.publishing_profile_id else None
    if pub is None:
        print(f"  FAIL  publishing_profiles — no row for publishing_profile_id={channel.publishing_profile_id!r}")
        ok = False
    else:
        cadence = pub.schedule.get("cadence", "?")
        day     = pub.schedule.get("day_of_week", "?")
        print(f"  PASS  publishing_profiles — platforms={pub.enabled_platforms}  cadence={cadence}/{day}")

    # 4. model_profiles
    mdl = get_model_profile(channel.model_profile_id) if channel.model_profile_id else None
    if mdl is None:
        print(f"  FAIL  model_profiles — no row for model_profile_id={channel.model_profile_id!r}")
        ok = False
    else:
        print(f"  PASS  model_profiles — ep={mdl.episode_producer!r}  voice={mdl.narration_voice_id!r}")

    # 5. load_channel_config (full join)
    cc = load_channel_config(CHANNEL_ID)
    if cc is None:
        print(f"  FAIL  load_channel_config — returned None")
        ok = False
    else:
        print(f"  PASS  load_channel_config — full ChannelConfig resolved")

    # 6. list_channel_profiles
    all_channels = list_channel_profiles()
    print(f"  INFO  list_channel_profiles — {len(all_channels)} channel(s) in DB")

    # 7. dashboard_api notifications — check no-channels notification is gone
    notifications = get_notifications()
    no_channel_notifs = [
        n for n in notifications
        if "no channels" in n.message.lower() or "create_channel" in n.message.lower()
    ]
    if no_channel_notifs:
        print(f"  FAIL  dashboard notifications — 'No channels configured' still present")
        ok = False
    else:
        print(f"  PASS  dashboard notifications — 'No channels configured' notification is gone")
        if notifications:
            print(f"        ({len(notifications)} other notification(s) present)")
        else:
            print(f"        (0 notifications — factory is clean)")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Hypogeum channel into live DB.")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip bootstrap; only run verification checks.",
    )
    args = parser.parse_args()

    config = Config()

    if not args.verify_only:
        if not _run_bootstrap(config):
            return 1

    if not _run_verification():
        print("\nVerification FAILED — check errors above.")
        return 1

    print("\nP1-01 complete. Hypogeum channel is in the DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
