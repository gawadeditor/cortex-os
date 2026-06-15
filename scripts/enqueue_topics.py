#!/usr/bin/env python3
"""
scripts/enqueue_topics.py — Topic Intake CLI

Enqueue episode topics into the topic backlog for a channel.
Topics are stored in the episode_topics table and claimed by the Queue Consumer
before each pipeline run.

Usage:
    # Single topic
    python3 scripts/enqueue_topics.py --channel-id hypogeum \
        --topic "The Antikythera Mechanism"

    # Multiple topics via CSV file
    python3 scripts/enqueue_topics.py --channel-id hypogeum \
        --csv topics.csv

    # With explicit priority (lower = higher urgency)
    python3 scripts/enqueue_topics.py --channel-id hypogeum \
        --topic "Breaking: New Discovery" --priority -1

    # Dry run: show what would be added without inserting
    python3 scripts/enqueue_topics.py --channel-id hypogeum \
        --csv topics.csv --dry-run

CSV format:
    One topic per line (plain text or single-column CSV).
    A first-line header named 'topic', 'topic_text', 'text', or 'title' is
    automatically skipped. Empty lines are silently skipped.

Exit codes:
    0 — all topics processed (some may have been skipped as duplicates)
    1 — hard error (channel not found, file not found, invalid arguments)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path when run directly
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hypogeum.topics import (
    ChannelNotFoundError,
    DuplicateTopicError,
    ImportResult,
    enqueue_topic,
    import_topics_from_csv,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enqueue episode topics into the Factory OS topic backlog.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--channel-id",
        required=True,
        metavar="CHANNEL_ID",
        help="Target channel identifier (must exist in channel_profiles).",
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--topic",
        metavar="TEXT",
        help="Single topic text to enqueue.",
    )
    source.add_argument(
        "--csv",
        metavar="FILE",
        help="Path to a CSV/text file with one topic per line.",
    )

    parser.add_argument(
        "--priority",
        type=int,
        default=0,
        metavar="N",
        help="Priority (lower = higher urgency). Default 0.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report without inserting anything.",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        help="Override database path (for testing/staging).",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path) if args.db_path else None

    if args.topic:
        return _enqueue_single(
            channel_id=args.channel_id,
            topic_text=args.topic,
            priority=args.priority,
            dry_run=args.dry_run,
            db_path=db_path,
        )
    else:
        return _enqueue_csv(
            channel_id=args.channel_id,
            csv_path=Path(args.csv),
            priority=args.priority,
            dry_run=args.dry_run,
            db_path=db_path,
        )


def _enqueue_single(
    channel_id: str,
    topic_text: str,
    priority: int,
    dry_run: bool,
    db_path: Path | None,
) -> int:
    if dry_run:
        print(f"[dry-run] Would enqueue: {topic_text!r} for channel '{channel_id}'")
        return 0

    try:
        topic_id = enqueue_topic(
            channel_id=channel_id,
            topic_text=topic_text,
            priority=priority,
            validate_channel=True,
            db_path=db_path,
        )
        print(f"Enqueued: {topic_text!r} (topic_id={topic_id})")
        return 0

    except ChannelNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    except DuplicateTopicError as exc:
        print(f"Skipped (duplicate): {topic_text!r}")
        print(f"  Reason: {exc}")
        return 0

    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _enqueue_csv(
    channel_id: str,
    csv_path: Path,
    priority: int,
    dry_run: bool,
    db_path: Path | None,
) -> int:
    if dry_run:
        # Validate channel and file exist, then show what would be imported
        try:
            from hypogeum.channel_config import get_channel_profile
            if get_channel_profile(channel_id, db_path=db_path) is None:
                print(f"Error: Channel '{channel_id}' not found.", file=sys.stderr)
                return 1
        except Exception as exc:
            print(f"Error checking channel: {exc}", file=sys.stderr)
            return 1

        if not csv_path.exists():
            print(f"Error: File not found: {csv_path}", file=sys.stderr)
            return 1

        lines = [
            ln.strip()
            for ln in csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
        print(f"[dry-run] Would process {len(lines)} line(s) from {csv_path}")
        print(f"[dry-run] Target channel: {channel_id}, priority: {priority}")
        return 0

    try:
        result: ImportResult = import_topics_from_csv(
            channel_id=channel_id,
            csv_path=csv_path,
            priority=priority,
            validate_channel=True,
            db_path=db_path,
        )
    except ChannelNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Import complete for channel '{channel_id}':")
    print(f"  Added:              {result.added}")
    print(f"  Skipped (duplicate): {result.skipped_duplicates}")
    print(f"  Skipped (empty):    {result.skipped_empty}")
    if result.errors:
        print(f"  Errors:             {len(result.errors)}", file=sys.stderr)
        for err in result.errors:
            print(f"    - {err}", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
