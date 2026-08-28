#!/usr/bin/env python3
"""
tools/regenerate_coverage_plan.py — CLI wrapper around
hypogeum.agents.cinematic_director.regenerate_cinematic_coverage_plan().

Isolated, planning-only Cinematic Coverage Plan regeneration for one
episode. Reads the episode's real script_package/narration_package/
visual_package from .hypogeum-assets/episodes/{episode_id}/, validates
narration completeness, and (only with --force) makes at most one LLM call
to regenerate the coverage plan under full backup/candidate/atomic-replace
discipline. Never touches Shot Diversity Planner, Stage B, any provider,
Assembly, Publishing, the database, or any of the episode's existing video
clips — see regenerate_cinematic_coverage_plan()'s own docstring for the
complete contract.

Usage:
    # Dry check: validates narration completeness only, makes no LLM call,
    # writes nothing. Exits non-zero if the checkpoint doesn't exist yet
    # (nothing to resume) or if narration is incomplete.
    python3 tools/regenerate_coverage_plan.py --episode-id ep_f3619022

    # Actual regeneration (one LLM call, backup + atomic replace):
    python3 tools/regenerate_coverage_plan.py --episode-id ep_f3619022 --force

    # Regeneration with the verified ep_f3619022 fixture totals enforced
    # (95 main / 35 short / 130 total / 47 sufficient) -- rejects the
    # candidate and preserves the current plan if the new plan's own
    # arithmetic doesn't reproduce them. This flag is intentionally the
    # ONLY place these four numbers are hardcoded anywhere in this feature
    # -- regenerate_cinematic_coverage_plan() itself has no built-in
    # per-episode expectations.
    python3 tools/regenerate_coverage_plan.py --episode-id ep_f3619022 --force \\
        --verify-ep-f3619022-fixture

    # First-batch feasibility probe (2026-08-09): submits at most ONE LLM
    # call, against batch 1 of the deterministic batch partition only,
    # through the exact same production request-construction code ordinary
    # regeneration uses. Never submits batch 2, never merges a full plan,
    # never writes a candidate, never installs or otherwise mutates the
    # active checkpoint, and creates no active-plan backup (nothing is
    # being installed, so there is nothing to protect). Prints
    # status=probe_succeeded or status=probe_rejected. Use this to cheaply
    # verify a batch's schema/request is actually accepted by the provider
    # before authorizing a full six-batch regeneration attempt.
    python3 tools/regenerate_coverage_plan.py --episode-id ep_f3619022 --force \\
        --verify-ep-f3619022-fixture --probe-first-batch
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config                                      # noqa: E402
from hypogeum.agents.cinematic_director import (                        # noqa: E402
    CoverageRegenerationError,
    NarrationCompletenessError,
    regenerate_cinematic_coverage_plan,
)

_EP_F3619022_FIXTURE = {
    "expected_main_shots": 95,
    "expected_short_shots": 35,
    "expected_total_shots": 130,
    "expected_sufficient_beats": 47,
}


def _load_json(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: required package not found: {path}", file=sys.stderr)
        sys.exit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--episode-id", required=True)
    p.add_argument("--force", action="store_true",
                   help="Actually regenerate (one LLM call). Without this, "
                        "only resumes/returns the cached plan or reports "
                        "that no checkpoint exists yet.")
    p.add_argument("--verify-ep-f3619022-fixture", action="store_true",
                   help="Enforce the verified ep_f3619022 totals "
                        "(95/35/130, 47/47 sufficient). Only meaningful for "
                        "that specific episode's real data.")
    p.add_argument("--probe-first-batch", action="store_true",
                   help="First-batch feasibility probe: at most ONE LLM call, "
                        "against batch 1 of the deterministic batch partition "
                        "only, through the exact production request path. "
                        "Never submits batch 2, never merges/installs/writes "
                        "a candidate, never creates a backup. Requires "
                        "--force. Ignored (has no effect) without --force.")
    p.add_argument("--assets-root", default=None,
                   help="Override .hypogeum-assets root (default: repo's own)")
    p.add_argument("--channel-id", default=None,
                   help="Authoritative channel id. REQUIRED with --force "
                        "(2026-08-26 plan-boundary rule): OpenRouter planning "
                        "spend is plan-scoped paid work -- the verified "
                        "persisted Production Plan's weekly reservation is "
                        "established/replayed before any LLM call, and a "
                        "denial fails closed. Never needed without --force.")
    args = p.parse_args()

    if args.force and not args.channel_id:
        p.error("--channel-id is REQUIRED with --force: OpenRouter planning "
                "spend is gated by the verified plan's weekly reservation")

    episode_id = args.episode_id
    assets_root = Path(args.assets_root) if args.assets_root else _ROOT / ".hypogeum-assets"
    episode_dir = assets_root / "episodes" / episode_id

    script_package = _load_json(episode_dir / f"script_package_{episode_id}.json")
    narration_package = _load_json(episode_dir / f"narration_package_{episode_id}.json")
    visual_package = _load_json(episode_dir / f"visual_package_{episode_id}.json")

    config = Config()

    fixture_verification = _EP_F3619022_FIXTURE if args.verify_ep_f3619022_fixture else None

    try:
        result = regenerate_cinematic_coverage_plan(
            script_package=script_package,
            narration_package=narration_package,
            visual_package=visual_package,
            episode_id=episode_id,
            config=config,
            output_dir=episode_dir,
            force=args.force,
            fixture_verification=fixture_verification,
            probe_first_batch=args.probe_first_batch,
            channel_id=args.channel_id,
        )
    except NarrationCompletenessError as exc:
        print(f"NARRATION COMPLETENESS GATE FAILED — no LLM call made.\n{exc}", file=sys.stderr)
        sys.exit(1)
    except CoverageRegenerationError as exc:
        print(f"REGENERATION NOT PERFORMED — {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"BATCH PARTITIONING FAILED — {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"status:            {result['status']}")
    print(f"reason:            {result['reason']}")
    print(f"active_plan_path:  {result['active_plan_path']}")
    print(f"backup_path:       {result['backup_path']}")
    print(f"candidate_path:    {result['candidate_path']}")
    print(f"totals:            {result['totals']}")
    if "probe_batch_index" in result:
        print(f"probe_batch_index: {result['probe_batch_index']} of {result['probe_batch_count']}")
        print(f"probe_batch_beats: {result['probe_batch_beat_ids']}")

    if result["status"] in ("rejected", "probe_rejected"):
        sys.exit(1)


if __name__ == "__main__":
    main()
