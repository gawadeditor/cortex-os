#!/usr/bin/env python3
"""
tools/configure_hypogeum_zernio.py

Turn on the Zernio cross-platform publishing transport for the `hypogeum`
channel so the pipeline (and the worker) publish every deliverable to
YouTube + Facebook + Instagram + TikTok through Zernio's unified release
flow instead of the direct YouTube-only client.

Requires the pipeline fix `106e621` (feat: route the pipeline's publishing
stage through Zernio for Zernio channels).

Sets:
  1. PublishingProfile.enabled_platforms -> [youtube, facebook, instagram, tiktok]
  2. PublishingProfile.publishing_provider_by_platform -> every platform "zernio"
  3. ChannelOperationsProfile.deliverable_bundle_definitions ->
     [{"bundle_id": "main_episode_bundle", "target_buffer_count": 1}]
     (a long-form episode + its Shorts, published as one unit -- FIX 4 needs
     a bundle definition to resolve what to hand Zernio)

SAFETY: dry-run by default. Does nothing without --apply AND an explicit
--db. Refuses .hypogeum-assets/hypogeum.db without
--i-understand-this-is-production.

    python3 tools/configure_hypogeum_zernio.py            # show the plan
    python3 tools/configure_hypogeum_zernio.py --apply --db /tmp/x.db
    python3 tools/configure_hypogeum_zernio.py --apply \
        --db .hypogeum-assets/hypogeum.db --i-understand-this-is-production

NOTE: TikTok's credential is currently an upload-only sandbox token
("no video.publish"); TikTok will receive the deliverable to its inbox,
not publish it. Confirm the TikTok privacy policy is installed in the DB
(channel_operations_profiles.platform_deliverable_policy_json) before
relying on live TikTok publishing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHANNEL_ID = "hypogeum"
_PROD_DB = (Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "hypogeum.db").resolve()

PLATFORMS = ["youtube", "facebook", "instagram", "tiktok"]
PROVIDER_BY_PLATFORM = {p: "zernio" for p in PLATFORMS}
BUNDLES = [{"bundle_id": "main_episode_bundle", "target_buffer_count": 1}]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    print("Planned changes for channel 'hypogeum':")
    print(f"  - publishing_profile.enabled_platforms            = {PLATFORMS}")
    print(f"  - publishing_profile.publishing_provider_by_platform = {PROVIDER_BY_PLATFORM}")
    print(f"  - channel_operations_profiles.deliverable_bundle_definitions = {BUNDLES}")
    print()

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply --db <path>.")
        return 0
    if args.db is None:
        print("ERROR: --apply requires an explicit --db <path>.", file=sys.stderr)
        return 2
    db = args.db.resolve()
    if db == _PROD_DB and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without "
              "--i-understand-this-is-production.", file=sys.stderr)
        return 2

    from hypogeum.channel_config import (
        get_channel_profile,
        get_channel_operations_profile,
        update_publishing_profile,
        update_channel_operations_profile,
    )

    cp = get_channel_profile(CHANNEL_ID, db_path=db)
    if cp is None or not getattr(cp, "publishing_profile_id", None):
        print(f"ERROR: no channel/publishing profile for {CHANNEL_ID!r} in {db}.", file=sys.stderr)
        return 1
    if get_channel_operations_profile(CHANNEL_ID, db_path=db) is None:
        print(f"ERROR: no ChannelOperationsProfile for {CHANNEL_ID!r} in {db}.", file=sys.stderr)
        return 1

    ok1 = update_publishing_profile(
        cp.publishing_profile_id, db_path=db,
        enabled_platforms=PLATFORMS,
        publishing_provider_by_platform=PROVIDER_BY_PLATFORM,
    )
    ok2 = update_channel_operations_profile(
        CHANNEL_ID, db_path=db, deliverable_bundle_definitions=BUNDLES,
    )
    print(f"Applied. publishing_profile updated={ok1}  operations_profile updated={ok2}")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
