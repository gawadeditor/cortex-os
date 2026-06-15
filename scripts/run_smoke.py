#!/usr/bin/env python3
"""
scripts/run_smoke.py — Factory OS Autonomous Runtime Smoke

Runs the end-to-end dry-run smoke path and exits 0 on success, 1 on failure.

    schedule → scheduled_run → run_queue → poll(dry_run=True)

Usage:
    python3 scripts/run_smoke.py
    python3 scripts/run_smoke.py --db-path /path/to/test.db

This script never calls run_pipeline, never generates content, and never
publishes. Safe to run in any environment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hypogeum.runtime import smoke_run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Factory OS dry-run smoke test for the full execution path."
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        help="Override database path (default: .hypogeum-assets/hypogeum.db).",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else None
    result = smoke_run(db_path=db_path)

    print(str(result))

    if result.success:
        print("\nSmoke test PASSED.")
        return 0
    else:
        print("\nSmoke test FAILED.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
