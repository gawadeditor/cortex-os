"""
scripts/generate_thumbnail_strategy.py

Generate a thumbnail_concept_brief for a given episode using the
Thumbnail Strategy Skill (Phase 1).

This script reads the topic_brief and script_package from disk, calls
the TSS skill, and writes thumbnail_concept_brief_{episode_id}.json to
the episode's asset directory.

Usage:
    python3 scripts/generate_thumbnail_strategy.py --episode ep_003

Requires:
    OPENROUTER_API_KEY in .env (loaded automatically via Config)

Output:
    .hypogeum-assets/episodes/<episode_id>/thumbnail_concept_brief_<episode_id>.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on path so hypogeum/ is importable
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config
from hypogeum.skills.thumbnail_strategy import run_thumbnail_strategy

_ASSETS_ROOT = _ROOT / ".hypogeum-assets" / "episodes"


def _load_json(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _find_package(episode_dir: Path, schema_prefix: str, episode_id: str) -> Path:
    """Find a JSON file by schema prefix, trying both naming conventions."""
    # Primary: schema_prefix_episode_id.json
    primary = episode_dir / f"{schema_prefix}_{episode_id}.json"
    if primary.exists():
        return primary
    # Fallback: any file matching the prefix
    matches = sorted(episode_dir.glob(f"{schema_prefix}*.json"))
    if matches:
        return matches[0]
    print(f"ERROR: could not find {schema_prefix} package in {episode_dir}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate thumbnail marketing concepts for an episode."
    )
    parser.add_argument(
        "--episode", "-e",
        required=True,
        help="Episode ID (e.g. ep_003)",
    )
    args = parser.parse_args()

    episode_id: str = args.episode
    episode_dir = _ASSETS_ROOT / episode_id

    if not episode_dir.exists():
        print(f"ERROR: episode directory not found: {episode_dir}", file=sys.stderr)
        sys.exit(1)

    # Load required inputs
    print(f"Loading topic_brief for {episode_id}...")
    topic_brief_path = _find_package(episode_dir, "topic_brief", episode_id)
    topic_brief = _load_json(topic_brief_path)
    print(f"  Loaded: {topic_brief_path.name}")

    print(f"Loading script_package for {episode_id}...")
    script_package_path = _find_package(episode_dir, "script_package", episode_id)
    script_package = _load_json(script_package_path)
    print(f"  Loaded: {script_package_path.name}")

    # Load config (reads .env automatically)
    print("Loading config...")
    try:
        config = Config()
    except Exception as exc:
        print(f"ERROR loading config: {exc}", file=sys.stderr)
        sys.exit(1)

    # Run the Thumbnail Strategy Skill
    print(f"\nRunning Thumbnail Strategy Skill for {episode_id}...")
    print(f"  Episode: {topic_brief.get('episode_title', episode_id)}")
    print(f"  Model:   {getattr(config, 'OPENROUTER_SONNET_MODEL_ID', 'anthropic/claude-sonnet-4.6')}")

    try:
        concept_brief = run_thumbnail_strategy(
            topic_brief=topic_brief,
            script_package=script_package,
            config=config,
            episode_id=episode_id,
            output_dir=episode_dir,
        )
    except Exception as exc:
        print(f"\nERROR: Thumbnail Strategy Skill failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Print summary
    output_path = episode_dir / f"thumbnail_concept_brief_{episode_id}.json"
    print(f"\nSUCCESS: thumbnail_concept_brief generated.")
    print(f"  Output: {output_path}")
    print(f"  Central question: {concept_brief.get('central_viewer_question', '')}")
    print()
    for c in concept_brief.get("concepts", []):
        print(f"  [{c['concept_id']}] {c['concept_name']}")
        print(f"    Trigger:    {c['emotional_trigger']}")
        print(f"    Hook text:  {c['suggested_hook_text']}")
        print(f"    Thesis:     {c['visual_thesis'][:80]}...")
        print()


if __name__ == "__main__":
    main()
