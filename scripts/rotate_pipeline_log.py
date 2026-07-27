#!/usr/bin/env python3
"""
scripts/rotate_pipeline_log.py — P0.1: One-time / on-demand pipeline.log
remediation.

Forces rotation of an oversized pipeline.log without waiting for a live
process's next write to cross the LOG_MAX_BYTES threshold. This is the
one-time remediation step for the log that had already grown to tens of
GB unrotated before Phase 3 (Production Hardening) shipped rotation
(hypogeum/logger.py). Safe to run against a live production log file:
reuses hypogeum.logger's rename-first rotation, so a concurrently
running process (e.g. run_worker.py) that reopens the path fresh on
every write is never left writing into a half-rotated or missing file.

Idempotent: running this against an already-small, already-rotated log
file is a harmless no-op unless --force is passed.

Usage:
    python3 scripts/rotate_pipeline_log.py
    python3 scripts/rotate_pipeline_log.py --log-file /path/to/other.log
    python3 scripts/rotate_pipeline_log.py --force   # rotate regardless of size

Exit codes:
    0 — rotation performed, or skipped because the file was already under
        the configured threshold (not an error)
    1 — the log file does not exist
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.logger import _DEFAULT_LOG_FILE, _rotate_log_file, _rotation_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-file",
        type=Path,
        default=_DEFAULT_LOG_FILE,
        help="Path to the log file to remediate (default: .hypogeum-assets/pipeline.log)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rotate even if the file is currently under the configured max size",
    )
    args = parser.parse_args()

    log_file: Path = args.log_file
    if not log_file.exists():
        print(f"No log file at {log_file} — nothing to remediate.")
        return 1

    max_bytes, backup_count = _rotation_settings()
    current_size = log_file.stat().st_size

    if not args.force and current_size <= max_bytes:
        print(
            f"{log_file} is {current_size:,} bytes, under the {max_bytes:,}-byte "
            "threshold — nothing to do. Pass --force to rotate anyway."
        )
        return 0

    print(f"Rotating {log_file} ({current_size:,} bytes) — backup_count={backup_count}")
    _rotate_log_file(log_file, backup_count)

    if log_file.exists():
        print(f"Done. {log_file} is now {log_file.stat().st_size:,} bytes.")
    else:
        print("Done. A fresh file will be created on the next write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
