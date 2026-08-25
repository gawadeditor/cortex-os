"""
scripts/recover_scenes.py — Targeted scene recovery (authorized channels
only)

Runs VPD Phase 2 (MUAPI generation only) for a specific set of scenes.
Resume safety in submit_scene() skips scenes whose clip files already exist.
Does NOT run Assembly, Publishing, or any other pipeline stage.

Phase 3 (2026-08-25): this tool is a live paid-video path and now requires
an authoritative --channel-id. With channel_id supplied, every paid
submission routes through the same enforced boundary as normal production:
verified persisted Production Plan -> grant/plan/finance-record three-way
agreement -> atomic CHANNEL_WEEKLY budget check-and-reserve ->
child authorization -> submission gate -> provider adapter. There is NO
ungated fallback: a missing/unknown channel, an unbound grant, or an
exhausted weekly budget fails closed before any network call.

Usage:
    # Path A — Runway retry (same prompts, T2):
    python3 scripts/recover_scenes.py --channel-id hypogeum \
        --episode-id ep_000_lite --scenes s02 s04

    # Path B — veo3-fast fallback (T3, if Runway fails again):
    python3 scripts/recover_scenes.py --channel-id hypogeum \
        --episode-id ep_000_lite --scenes s02 s04 --force-tier 3

Options:
    --channel-id   Authoritative channel identifier (REQUIRED; the grant,
                   weekly budget, and channel package are resolved against it)
    --episode-id   Episode identifier (required)
    --scenes       Scene IDs to target (space-separated; others skipped via resume safety)
    --force-tier   Override primary_tier for targeted scenes (2=runway, 3=veo3-fast)
    --budget       Episode budget ceiling in credits (default: 2.00)
    --dry-run      Dry-run only (set MUAPI_DRY_RUN=true via env to use this)

Budget guard is per-process. Prior run spend is not counted by the legacy
in-process guard; the DB-enforced CHANNEL_WEEKLY scope and grant ledger
count every enforced submission regardless of process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from hypogeum.agents.visual_package_director import run_visual_package_director_from_plan
from hypogeum.config import Config


def _load_env() -> None:
    """Load .env from project root if present."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _load_visual_package(episode_id: str, assets_root: Path) -> dict:
    path = assets_root / "episodes" / episode_id / f"visual_package_{episode_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"visual_package not found: {path}")
    with open(path) as f:
        return json.load(f)


def _apply_tier_override(visual_package: dict, target_scene_ids: list[str], tier: int) -> None:
    """
    In-memory override of primary_tier for targeted scenes.
    Updates primary_tier only; prompts are preserved.
    Called only when --force-tier is set.
    """
    tier_map = {1: "T1", 2: "T2", 3: "T3"}
    for scene in visual_package.get("scenes", []):
        if scene.get("scene_id") in target_scene_ids:
            routing = scene.get("production_spec", {}).get("provider_routing", {})
            original = routing.get("primary_tier")
            routing["primary_tier"] = tier
            print(
                f"  [{scene['scene_id']}] tier override: {original} → {tier} ({tier_map.get(tier, '?')})"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Targeted scene recovery for Hypogeum pipeline.")
    parser.add_argument("--channel-id", required=True, dest="channel_id",
                        help="Authoritative channel identifier (REQUIRED -- paid recovery "
                             "submissions are enforced against this channel's grant and "
                             "weekly budget; no ungated fallback exists)")
    parser.add_argument("--episode-id", required=True, help="Episode identifier, e.g. ep_000_lite")
    parser.add_argument("--scenes", nargs="+", required=True, help="Scene IDs to target, e.g. s02 s04")
    parser.add_argument("--force-tier", type=int, choices=[1, 2, 3], default=None,
                        help="Override primary_tier for targeted scenes (2=runway, 3=veo3-fast)")
    parser.add_argument("--budget", type=float, default=2.00,
                        help="Episode budget ceiling in credits (default: 2.00)")
    args = parser.parse_args()

    _load_env()

    # Fail-closed guard independent of argparse (also covers programmatic
    # callers): a blank channel_id must never reach a paid boundary.
    channel_id = (args.channel_id or "").strip()
    if not channel_id:
        print("ERROR: --channel-id is required: paid scene recovery without an "
              "authoritative channel context is not permitted.", file=sys.stderr)
        return 1

    episode_id = args.episode_id
    target_scenes = args.scenes

    # Set budget ceiling for this recovery process
    os.environ["MUAPI_EPISODE_BUDGET_CREDITS"] = str(args.budget)

    assets_root = Path(__file__).parent.parent / ".hypogeum-assets"
    output_dir = assets_root / "episodes" / episode_id

    print(f"\nSCENE RECOVERY — {episode_id}")
    print("=" * 50)
    print(f"Channel:         {channel_id} (enforced: plan/grant/finance/weekly budget)")
    print(f"Target scenes:   {', '.join(target_scenes)}")
    print(f"Budget ceiling:  ${args.budget:.2f}")
    if args.force_tier:
        print(f"Tier override:   T{args.force_tier}")
    print(f"MUAPI_DRY_RUN:   {os.environ.get('MUAPI_DRY_RUN', 'false')}")
    print()

    # Verify existing clips (resume safety will skip these)
    video_dir = output_dir / "video"
    print("Existing clips (will be skipped by resume safety):")
    for mp4 in sorted(video_dir.glob("sc_*.mp4")):
        size_mb = mp4.stat().st_size / 1_048_576
        print(f"  {mp4.name}  ({size_mb:.1f} MB)")
    print()

    # Load config
    try:
        config = Config()
    except Exception as e:
        print(f"ERROR: Config load failed: {e}", file=sys.stderr)
        return 1

    # Load existing visual package
    print("Loading visual_package...")
    try:
        visual_package = _load_visual_package(episode_id, assets_root)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"  {len(visual_package.get('scenes', []))} scenes planned.")
    print()

    # Apply tier override if requested
    if args.force_tier is not None:
        print(f"Applying T{args.force_tier} tier override to: {', '.join(target_scenes)}")
        _apply_tier_override(visual_package, target_scenes, args.force_tier)
        print()

    # Run VPD Phase 2 only — resume safety handles existing clips.
    # channel_id is ALWAYS supplied (required above): every paid submission
    # below goes through the enforced gate path -- verified persisted plan,
    # grant/plan/finance three-way agreement, atomic CHANNEL_WEEKLY
    # reservation, child authorization, submission gate. A denial at any
    # layer surfaces as a per-scene generation failure, never a fallback.
    print("Running VPD Phase 2 (generation only)...")
    print("  Scenes not in target list will be skipped by resume safety if files exist.")
    print()
    try:
        result = run_visual_package_director_from_plan(
            visual_package=visual_package,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            channel_id=channel_id,
        )
    except Exception as e:
        print(f"ERROR during generation: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # Report results
    print()
    print("RECOVERY RESULTS")
    print("-" * 40)
    assets = result.get("asset_manifest", {}).get("assets", [])
    all_generated = True
    for asset in assets:
        sid = asset.get("scene_id", "?")
        status = asset.get("generation_status", "?")
        aid = asset.get("asset_id", "")
        if sid in target_scenes:
            file_exists = False
            if aid:
                p = assets_root / f"{aid}.mp4"
                file_exists = p.exists() and p.stat().st_size > 0
            print(f"  {sid}: {status}  asset_id={aid!r}  file_exists={file_exists}")
            if status not in ("generated", "reused", "dry_run"):
                all_generated = False

    print()
    if all_generated:
        print("SUCCESS — both target scenes recovered.")
        print("Next step: run Assembly.")
        print()
        print("  python3 -c \"")
        print("  import json; from pathlib import Path")
        print("  # ... run_assembly_agent with updated visual_package")
        print("  \"")
    else:
        print("PARTIAL FAILURE — one or more scenes still failed.")
        print("If Runway returned 400 again for the same scenes:")
        print("  Re-run with --force-tier 3 to route to veo3-fast.")
        print()
        print(f"  python3 scripts/recover_scenes.py --channel-id {channel_id} \\")
        print(f"      --episode-id {episode_id} \\")
        print(f"      --scenes {' '.join(target_scenes)} --force-tier 3 --budget 2.00")

    return 0 if all_generated else 1


if __name__ == "__main__":
    sys.exit(main())
