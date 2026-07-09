"""
tools/build_selective_regeneration_plan.py

Patches the visual_package asset_manifest so that Phase 2 (MUAPI generation)
runs ONLY for the scenes that genuinely need new clips.

Categories:
  --reuse-scenes           Clip from reference episode is good enough.
                           Sets asset_id → ref episode path, generation_status → "reused".
                           Assembly Agent resolves: assets_root / f"{asset_id}.mp4"
                           which is .hypogeum-assets/episodes/<ref_id>/video/sc_<sid>.mp4

  --already-generated-scenes  New clip already on disk in this episode's video dir.
                           Sets generation_status → "generated".
                           asset_id unchanged (already points at reg episode path).

  --regenerate-scenes      Must generate a new clip via MUAPI Phase 2.
                           Sets generation_status → "generation_failed".
                           Pipeline re-runs Phase 2 only for these.

  --dry-run                Print plan; do not write anything.

Usage:
  python3 tools/build_selective_regeneration_plan.py \\
    --episode-id ep_reg_vi_colosseum \\
    --reference-episode-id ep_20260629075717_c7abfd \\
    --reuse-scenes s02,s05,s07,s10,s14,s17,s26,s28,s31 \\
    --already-generated-scenes s13,s16 \\
    --regenerate-scenes s01,s03,s04,s06,s08,s09,s11,s12,s15,s18,s19,s20,s21,s22,s23,s24,s25,s27,s29,s30,s32,s33 \\
    --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


ASSETS_ROOT = Path(".hypogeum-assets")
COST_PER_CLIP = 0.60


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--reference-episode-id", required=True)
    parser.add_argument("--reuse-scenes", default="", help="Comma-separated scene_ids to reuse from reference")
    parser.add_argument("--already-generated-scenes", default="", help="Comma-separated scene_ids already on disk")
    parser.add_argument("--regenerate-scenes", default="", help="Comma-separated scene_ids that need new clips")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    args = parser.parse_args()

    episode_id = args.episode_id
    ref_id = args.reference_episode_id

    reuse_scenes = set(s.strip() for s in args.reuse_scenes.split(",") if s.strip())
    already_generated = set(s.strip() for s in args.already_generated_scenes.split(",") if s.strip())
    regenerate_scenes = set(s.strip() for s in args.regenerate_scenes.split(",") if s.strip())

    # Validate no overlap
    overlap = (reuse_scenes & already_generated) | (reuse_scenes & regenerate_scenes) | (already_generated & regenerate_scenes)
    if overlap:
        print(f"ERROR: scene_ids appear in multiple categories: {sorted(overlap)}", file=sys.stderr)
        sys.exit(1)

    all_declared = reuse_scenes | already_generated | regenerate_scenes

    # Load visual_package
    vp_path = ASSETS_ROOT / "episodes" / episode_id / f"visual_package_{episode_id}.json"
    if not vp_path.exists():
        print(f"ERROR: visual_package not found at {vp_path}", file=sys.stderr)
        sys.exit(1)

    vp = json.loads(vp_path.read_text())
    assets: list[dict] = vp.get("asset_manifest", {}).get("assets", [])

    print(f"{'DRY RUN — ' if args.dry_run else ''}Selective regeneration plan")
    print(f"  Episode:   {episode_id}")
    print(f"  Reference: {ref_id}")
    print(f"  REUSE:     {len(reuse_scenes)} scenes")
    print(f"  GENERATED: {len(already_generated)} scenes (already on disk)")
    print(f"  REGEN:     {len(regenerate_scenes)} scenes → ${len(regenerate_scenes) * COST_PER_CLIP:.2f}")
    print()

    problems: list[str] = []
    rows: list[tuple] = []  # (scene_id, category, old_status, new_status, old_asset_id, new_asset_id, ok, note)

    for asset in assets:
        sid = asset.get("scene_id", "")
        source_type = asset.get("source_type", "")

        # Skip short-episode scenes — not in scope for this operation
        if source_type != "main_episode":
            rows.append((sid, "SKIP_SHORT", asset.get("generation_status"), asset.get("generation_status"),
                         asset.get("asset_id"), asset.get("asset_id"), True, "short scene — untouched"))
            continue

        if sid not in all_declared:
            rows.append((sid, "UNDECLARED", asset.get("generation_status"), asset.get("generation_status"),
                         asset.get("asset_id"), asset.get("asset_id"), False,
                         "WARNING: scene not in any category — left unchanged"))
            problems.append(f"Undeclared main scene: {sid}")
            continue

        if sid in reuse_scenes:
            # Point asset_id at reference episode canonical clip
            ref_asset_id = f"episodes/{ref_id}/video/sc_{sid}"
            ref_clip = ASSETS_ROOT / f"{ref_asset_id}.mp4"
            ok = ref_clip.exists()
            note = "ref clip found" if ok else f"MISSING: {ref_clip}"
            if not ok:
                problems.append(f"Reuse clip missing for {sid}: {ref_clip}")
            rows.append((sid, "REUSE", asset.get("generation_status"), "reused",
                         asset.get("asset_id"), ref_asset_id, ok, note))

        elif sid in already_generated:
            # Clip should already exist in reg episode video dir
            cur_asset_id = asset.get("asset_id", f"episodes/{episode_id}/video/sc_{sid}")
            reg_clip = ASSETS_ROOT / f"{cur_asset_id}.mp4"
            ok = reg_clip.exists()
            note = "clip found on disk" if ok else f"MISSING: {reg_clip}"
            if not ok:
                problems.append(f"Already-generated clip missing for {sid}: {reg_clip}")
            rows.append((sid, "ALREADY_GENERATED", asset.get("generation_status"), "generated",
                         cur_asset_id, cur_asset_id, ok, note))

        elif sid in regenerate_scenes:
            # Mark as generation_failed so Phase 2 submits it to MUAPI
            cur_asset_id = asset.get("asset_id", f"episodes/{episode_id}/video/sc_{sid}")
            rows.append((sid, "REGENERATE", asset.get("generation_status"), "generation_failed",
                         cur_asset_id, cur_asset_id, True, "will be submitted to MUAPI"))

    # Print table
    header = f"{'Scene':<8} {'Category':<18} {'Old Status':<20} {'New Status':<20} {'OK':<4} Note"
    print(header)
    print("-" * len(header))
    for sid, cat, old_st, new_st, old_aid, new_aid, ok, note in rows:
        if cat == "SKIP_SHORT":
            continue  # keep output clean
        ok_str = "✓" if ok else "✗"
        print(f"{sid:<8} {cat:<18} {str(old_st):<20} {new_st:<20} {ok_str:<4} {note}")
        if new_aid != old_aid:
            print(f"         asset_id: {old_aid}")
            print(f"              → {new_aid}")

    print()
    if problems:
        print(f"PROBLEMS ({len(problems)}):")
        for p in problems:
            print(f"  ✗ {p}")
        print()

    # Count outcome
    regen_count = sum(1 for r in rows if r[1] == "REGENERATE")
    reuse_count = sum(1 for r in rows if r[1] == "REUSE" and r[6])
    done_count  = sum(1 for r in rows if r[1] == "ALREADY_GENERATED" and r[6])
    print(f"Outcome: {reuse_count} REUSE, {done_count} ALREADY_GENERATED, {regen_count} REGENERATE")
    print(f"Estimated cost: {regen_count} × ${COST_PER_CLIP:.2f} = ${regen_count * COST_PER_CLIP:.2f}")

    if args.dry_run:
        print()
        print("DRY RUN — visual_package NOT modified. Re-run without --dry-run to apply.")
        if problems:
            sys.exit(1)
        return

    if problems:
        print()
        print("Aborting: resolve problems before applying.", file=sys.stderr)
        sys.exit(1)

    # --- Apply ---
    # Backup first
    backup_path = vp_path.with_suffix(".json.salvage_bak")
    shutil.copy2(str(vp_path), str(backup_path))
    print(f"\nBackup written: {backup_path}")

    # Build lookup: scene_id → row data
    row_by_sid: dict[str, tuple] = {r[0]: r for r in rows}

    for asset in assets:
        sid = asset.get("scene_id", "")
        if sid not in row_by_sid:
            continue
        _, cat, _, new_status, _, new_asset_id, ok, _ = row_by_sid[sid]
        if cat == "SKIP_SHORT" or cat == "UNDECLARED":
            continue
        asset["generation_status"] = new_status
        if cat == "REUSE":
            asset["asset_id"] = new_asset_id

    vp_path.write_text(json.dumps(vp, indent=2))
    print(f"Written: {vp_path}")
    print()
    print(f"Phase 2 will now generate only the {regen_count} REGENERATE scenes.")
    print("Run pipeline to execute:")
    print(f"  python3 run_pipeline.py --topic '...' --episode-id {episode_id} "
          f"--channel-id hypogeum --youtube-privacy private")


if __name__ == "__main__":
    main()
