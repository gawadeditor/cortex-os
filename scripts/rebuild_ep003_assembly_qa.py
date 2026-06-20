"""
scripts/rebuild_ep003_assembly_qa.py

Targeted rebuild: re-run Assembly + QA on ep_003 using existing on-disk artifacts.
Does NOT call any generation APIs (video, TTS, music already present on disk).

Run from the repo root:
    python3 scripts/rebuild_ep003_assembly_qa.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure repo root is on path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.agents.assembly_agent import run_assembly_agent
from hypogeum.agents.qa_agent import run_qa_agent
from hypogeum.config import Config

EPISODE_ID = "ep_003"
ASSETS_ROOT = _ROOT / ".hypogeum-assets"
EPISODE_DIR = ASSETS_ROOT / "episodes" / EPISODE_ID


def _load(schema: str) -> dict:
    path = EPISODE_DIR / f"{schema}_{EPISODE_ID}.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing: {path}")
    with open(path) as f:
        return json.load(f)


def _make_config() -> Config:
    # Config only needs API keys for logging; no generation happens here.
    env = {
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "sk-placeholder"),
        "ELEVENLABS_API_KEY": os.environ.get("ELEVENLABS_API_KEY", "xi-placeholder"),
        "ELEVENLABS_VOICE_ID": os.environ.get("ELEVENLABS_VOICE_ID", "voice-placeholder"),
    }
    orig = {}
    for k, v in env.items():
        orig[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        cfg = Config()
    finally:
        for k, v in orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return cfg


def _build_music_track_package() -> dict:
    """
    Build a pre-filled music_track_package from the existing library track.
    This bypasses any API call — uses the on-disk Mystery mp3 directly.
    """
    library_path = ASSETS_ROOT / "channel" / "music" / "music-library.json"
    with open(library_path) as f:
        lib = json.load(f)

    mystery_track = next(
        (t for t in lib.get("tracks", []) if t.get("category") == "Mystery"),
        None,
    )
    if mystery_track is None:
        print("WARNING: no Mystery track in library — assembly will skip music")
        return {
            "package_id": "mtp_ep003_rebuild",
            "sourcing_status": "failed",
            "tracks": {},
            "music_log": [],
            "sections_without_music": ["main_episode"],
        }

    asset_id = mystery_track["path"]
    local_path = ASSETS_ROOT / f"{asset_id}.mp3"

    return {
        "package_id": "mtp_ep003_rebuild",
        "sourcing_status": "complete",
        "tracks": {
            "main_episode": {
                "section_key": "main_episode",
                "category": "Mystery",
                "asset_id": asset_id,
                "local_path": str(local_path),
                "source": "channel_library",
                "duration_seconds": mystery_track["duration_seconds"],
                "prompt_used": mystery_track.get("prompt_used", ""),
            }
        },
        "music_log": [],
        "sections_without_music": [],
    }


def main() -> None:
    print(f"\n{'='*60}")
    print(f"  ep_003 Assembly + QA Rebuild")
    print(f"{'='*60}\n")

    # --- Load existing packages ---
    print("Loading existing ep_003 packages from disk...")
    visual_package   = _load("visual_package")
    narration_package = _load("narration_package")
    thumbnail_package = _load("thumbnail_package")
    script_package   = _load("script_package")
    topic_brief      = _load("topic_brief")
    research_package = _load("research_package")
    source_registry  = research_package.get("source_registry", [])
    print(f"  visual_package:    {len(visual_package.get('scenes', []))} scenes, "
          f"{sum(1 for a in visual_package.get('asset_manifest', {}).get('assets', []) if a.get('asset_id'))} with asset_id")
    print(f"  narration_package: {len(narration_package.get('narration_segments', []))} segments, "
          f"{len(narration_package.get('stitched_tracks', []))} stitched tracks")
    print(f"  thumbnail_package: present")
    print(f"  topic_brief:       {topic_brief.get('episode_title', '(missing)')}")

    # --- Count available clips ---
    clips_on_disk = []
    clips_missing = []
    for asset in visual_package.get("asset_manifest", {}).get("assets", []):
        aid = asset.get("asset_id")
        if aid:
            p = ASSETS_ROOT / f"{aid}.mp4"
            if p.exists() and p.stat().st_size > 0:
                clips_on_disk.append(aid)
            else:
                clips_missing.append(asset.get("scene_id", aid))

    total_scenes = len(visual_package.get("scenes", []))
    rate = len(clips_on_disk) / total_scenes if total_scenes else 0
    print(f"\nClip availability: {len(clips_on_disk)}/{total_scenes} = {rate:.1%}")
    print(f"Missing clips: {clips_missing}")

    # --- Build music package from library (no API call) ---
    music_track_package = _build_music_track_package()
    print(f"\nMusic: {music_track_package['sourcing_status']} "
          f"(sections: {list(music_track_package['tracks'].keys())})")

    # --- Run Assembly Agent ---
    print(f"\n{'─'*60}")
    print("Running Assembly Agent...")
    config = _make_config()

    final_render_package = run_assembly_agent(
        visual_package=visual_package,
        narration_package=narration_package,
        thumbnail_package=thumbnail_package,
        config=config,
        episode_id=EPISODE_ID,
        output_dir=EPISODE_DIR,
        script_package=script_package,
        source_registry=source_registry,
        topic_brief=topic_brief,
        music_track_package=music_track_package,
    )

    # --- Report Assembly result ---
    pkg_integrity = final_render_package.get("package_integrity", {})
    ep_render = final_render_package.get("episode_render", {})
    meta = final_render_package.get("metadata_source", {})
    qa_report = final_render_package.get("assembly_qa_report", {})

    print(f"\nAssembly result:")
    print(f"  production_ready:     {pkg_integrity.get('production_ready')}")
    print(f"  video_asset_id:       {ep_render.get('video_asset_id', '(empty)')!r}")
    print(f"  caption_asset_id:     {ep_render.get('caption_asset_id', '(empty)')!r}")
    print(f"  episode_title:        {meta.get('episode_title', '(empty)')!r}")
    print(f"  description:          {meta.get('description', '(empty)')[:80]!r}...")
    print(f"  captions_included:    {pkg_integrity.get('captions_included')}")
    print(f"  assembly_qa_status:   {qa_report.get('overall_status')}")
    print(f"  clip_availability:    {qa_report.get('clip_availability_rate', 0):.1%}")
    print(f"  scenes_missing:       {qa_report.get('scenes_missing', [])}")
    print(f"  render_package_id:    {final_render_package.get('package_metadata', {}).get('render_package_id')}")

    # --- Run QA Agent ---
    print(f"\n{'─'*60}")
    print("Running QA Agent...")

    thumbnail_package_full = _load("thumbnail_package")

    qa_approval = run_qa_agent(
        final_render_package=final_render_package,
        episode_id=EPISODE_ID,
        thumbnail_package=thumbnail_package_full,
        output_dir=EPISODE_DIR,
    )

    print(f"\nQA result:")
    print(f"  verdict:     {qa_approval.get('verdict')}")
    print(f"  approved:    {qa_approval.get('approved')}")

    print(f"\nQA checks:")
    for check in qa_approval.get("checks", []):
        icon = "✓" if check["status"] == "pass" else ("⚠" if check["status"] == "warn" else "✗")
        print(f"  {icon} [{check['status']:4s}] {check['check_id']}: {check['detail']}")

    if qa_approval.get("failures"):
        print(f"\nFAILURES ({len(qa_approval['failures'])}):")
        for f in qa_approval["failures"]:
            print(f"  ✗ {f}")

    if qa_approval.get("warnings"):
        print(f"\nWarnings ({len(qa_approval['warnings'])}):")
        for w in qa_approval["warnings"]:
            print(f"  ⚠ {w}")

    print(f"\n{'='*60}")
    if qa_approval.get("approved"):
        print(f"  RESULT: PASSED — ep_003 is production-ready (verdict={qa_approval['verdict']})")
    else:
        print(f"  RESULT: FAILED — ep_003 blocked (verdict={qa_approval['verdict']})")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
