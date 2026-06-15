#!/usr/bin/env python3
"""
run_pipeline.py — Pipeline CLI entry point

Invokes the full 9-agent hypogeum-brain pipeline for one episode.

Usage:
    python run_pipeline.py --topic "The Antikythera Mechanism" --episode-id ep_001
    python run_pipeline.py --topic "The Antikythera Mechanism" --episode-id ep_001 --channel-id hypogeum

Options:
    --topic       Topic idea string passed to the Executive Producer. Required.
    --episode-id  Unique episode identifier used for log correlation and file naming. Required.
    --channel-id  Channel ID to load from DB. Default: 'hypogeum'.
                  Model assignments and voice ID are loaded from the DB channel
                  profile and applied on top of .env defaults.
                  The channel must exist and be active before the pipeline starts.
    --output-dir  Override output directory for all agent files.
                  Default: .hypogeum-assets/episodes/{episode_id}/

Exit codes:
    0  — pipeline completed (Publishing Record sealed)
    1  — topic rejected by Executive Producer (decision=REJECT; review scorecard)
    2  — pipeline halted by Package Deficiency Report (resolve and re-run)
    3  — startup failure — no viable API path (check credentials and connectivity)
    4  — unhandled error (channel not found, configuration, agent, or runtime failure)

Notes:
    - All Claude calls route through OpenRouter (primary) or Anthropic direct (fallback).
    - Set OPENROUTER_API_KEY in .env before running.
    - Run scripts/bootstrap_channel.py before using --channel-id.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hypogeum.config import Config
from hypogeum.pipeline import (
    ChannelNotActiveError,
    ChannelNotFoundError,
    PackageDeficiencyError,
    PipelineRejectedError,
    run_pipeline,
)
from hypogeum.startup import StartupError


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_pipeline",
        description="hypogeum-brain — automated YouTube production pipeline",
    )
    parser.add_argument(
        "--topic",
        required=True,
        metavar="TOPIC",
        help="Topic idea for the episode (e.g. 'The Antikythera Mechanism')",
    )
    parser.add_argument(
        "--episode-id",
        required=True,
        metavar="EPISODE_ID",
        help="Unique episode identifier (e.g. 'ep_001')",
    )
    parser.add_argument(
        "--channel-id",
        default="hypogeum",
        metavar="CHANNEL_ID",
        help=(
            "Channel ID to load configuration from DB. Default: 'hypogeum'. "
            "Model assignments and voice ID are read from the channel profile "
            "in the DB and applied on top of .env defaults. "
            "The channel must exist and be active before the pipeline starts."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="Override output directory for all agent files",
    )
    parser.add_argument(
        "--youtube-privacy",
        default="public",
        choices=["public", "private", "unlisted"],
        help="YouTube privacy for all uploads (default: public). Use 'private' for Episode Zero.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else None

    try:
        config = Config()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 4

    try:
        result = run_pipeline(
            topic_input=args.topic,
            episode_id=args.episode_id,
            config=config,
            output_dir=output_dir,
            youtube_privacy=args.youtube_privacy,
            channel_id=args.channel_id,
        )
    except ChannelNotFoundError as exc:
        print(f"Channel not found: {exc}", file=sys.stderr)
        return 4
    except ChannelNotActiveError as exc:
        print(f"Channel not active: {exc}", file=sys.stderr)
        return 4
    except StartupError as exc:
        print(f"Startup failure: {exc}", file=sys.stderr)
        return 3
    except PipelineRejectedError as exc:
        print(f"Topic rejected: {exc}", file=sys.stderr)
        return 1
    except PackageDeficiencyError as exc:
        print(f"Package deficiency: {exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"Not implemented: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"Pipeline error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 4

    print(
        f"Pipeline completed: episode_id={result.episode_id!r}, "
        f"status={result.status!r}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
