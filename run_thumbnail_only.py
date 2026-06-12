"""
run_thumbnail_only.py — Standalone thumbnail regeneration for completed episodes.

Reads the existing visual_package for a given episode, rebuilds the
thumbnail handoff brief, and runs the full Thumbnail Director workflow
(Phase 1 Claude planning + Phase 2 MUAPI generation + QA).

Does NOT re-run VPD, audio, video, or any other pipeline stage.

Usage:
    python3 run_thumbnail_only.py --episode-id ep_smoke_01
    python3 run_thumbnail_only.py --episode-id ep_smoke_01 --from-existing-plan
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hypogeum.agents.thumbnail_director import run_thumbnail_director
from hypogeum.agents.thumbnail_handoff_brief_builder import build_thumbnail_handoff_brief
from hypogeum.agents.thumbnail_identity_guardian import run_thumbnail_identity_guardian
from hypogeum.agents.thumbnail_qa import run_thumbnail_qa
from hypogeum.config import Config


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate thumbnails for a completed episode")
    parser.add_argument("--episode-id", required=True, metavar="EPISODE_ID")
    parser.add_argument(
        "--from-existing-plan",
        action="store_true",
        help="Skip Claude Phase 1 and use the existing generation plan on disk",
    )
    args = parser.parse_args()

    episode_id = args.episode_id

    try:
        config = Config()
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 4

    assets_root = Path(__file__).resolve().parent / ".hypogeum-assets" / "episodes" / episode_id
    if not assets_root.exists():
        print(f"Episode directory not found: {assets_root}", file=sys.stderr)
        return 2

    vp_path = assets_root / f"visual_package_{episode_id}.json"
    if not vp_path.exists():
        print(f"visual_package not found: {vp_path}", file=sys.stderr)
        return 2

    with open(vp_path) as f:
        visual_package = json.load(f)

    print(f"\n{'='*60}")
    print(f"THUMBNAIL REGENERATION — {episode_id}")
    print(f"{'='*60}")

    # Build handoff brief
    print("\n[1/4] Building thumbnail handoff brief...")
    handoff_brief = build_thumbnail_handoff_brief(
        visual_package=visual_package,
        episode_id=episode_id,
        output_dir=assets_root,
    )
    print(f"      Candidates: {len(handoff_brief.get('candidates', []))}")
    print(f"      Short candidates: {len(handoff_brief.get('short_candidates', []))}")

    # Run thumbnail director
    print("\n[2/4] Running Thumbnail Director (Phase 1 Claude + Phase 2 MUAPI)...")
    print(f"      BGA enabled: {config.BGA_ENABLED}")
    thumbnail_package = run_thumbnail_director(
        handoff_brief=handoff_brief,
        config=config,
        episode_id=episode_id,
        output_dir=assets_root,
    )

    # Show generation plan
    plan_path = assets_root / f"thumbnail_generation_plan_{episode_id}.json"
    if plan_path.exists():
        with open(plan_path) as f:
            plan = json.load(f)
        print("\n[3/4] Generation Plan:")
        main_ep = plan.get("main_episode", {})
        for c in main_ep.get("ranked_candidates", []):
            overlay = c.get("text_overlay")
            overlay_str = f'"{overlay["text"]}" ({overlay["placement"]})' if overlay else "null"
            print(f"      Rank {c['rank']} [{c['candidate_id']}]:")
            print(f"        Prompt: {c['image_prompt'][:80]}...")
            print(f"        Text overlay: {overlay_str}")
            print(f"        Aspect ratio: {c['aspect_ratio']}")

    # Run thumbnail QA manually and show full report
    print("\n[4/4] Thumbnail QA Report:")
    main_thumb = assets_root / "thumbnails" / "main.jpg"
    if main_thumb.exists():
        from PIL import Image
        img = Image.open(main_thumb)
        print(f"      File: {main_thumb}")
        print(f"      Dimensions: {img.size[0]}×{img.size[1]}")
        print(f"      Expected: 1280×720 (16:9)")

    qa_report = thumbnail_package.get("thumbnail_qa_report", {})
    if qa_report:
        print(f"\n      QA Overall Status: {qa_report.get('overall_status', 'unknown').upper()}")
        for check in qa_report.get("checks", []):
            status_icon = "✓" if check["status"] == "pass" else ("⚠" if check["status"] == "warn" else "✗")
            print(f"      {status_icon} {check['check_id']}: {check['detail']}")
        if qa_report.get("failures"):
            print("\n      FAILURES:")
            for f in qa_report["failures"]:
                print(f"        - {f}")
        if qa_report.get("warnings"):
            print("\n      WARNINGS:")
            for w in qa_report["warnings"]:
                print(f"        - {w}")
    else:
        print("      (no QA report in thumbnail_package)")

    # Run Thumbnail Identity Guardian (vision-based post-generation inspection)
    print("\n[5/5] Thumbnail Identity Guardian (TIG):")
    if main_thumb.exists():
        tig_report = run_thumbnail_identity_guardian(
            episode_id=episode_id,
            image_path=main_thumb,
            config=config,
            output_dir=assets_root / "tig",
        )
        verdict = tig_report.get("verdict", "unknown").upper()
        print(f"      Verdict: {verdict}")
        if tig_report.get("blocking_count", 0):
            print(f"      BLOCKING violations: {tig_report['blocking_count']}")
        if tig_report.get("warning_count", 0):
            print(f"      Warnings: {tig_report['warning_count']}")
        for v in tig_report.get("violations", []):
            icon = "✗" if v["severity"] == "BLOCKING" else "⚠"
            print(f"      {icon} [{v['rule_id']}] {v['description']}")
    else:
        print("      (main thumbnail not found — skipping TIG)")

    print(f"\n{'='*60}")
    print(f"RESULT: production_ready={thumbnail_package.get('package_integrity', {}).get('production_ready')}")
    print(f"  Main asset: {thumbnail_package.get('main_thumbnail', {}).get('asset_id')}")
    print(f"  Short thumbnails: {len(thumbnail_package.get('short_thumbnails', []))}")
    print(f"{'='*60}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
