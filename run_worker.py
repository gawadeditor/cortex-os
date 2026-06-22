#!/usr/bin/env python3
"""
run_worker.py — Factory OS Autonomous Worker

Drives the full scheduler → queue → pipeline execution loop.

Each iteration:
  1. scheduler.tick()              — enqueue pending scheduled_run rows for due schedules
  2. consume_scheduled_runs()      — promote pending scheduled_runs to run_queue
  3. queue.poll(max_iterations=1)  — claim one queued item; run pipeline or dry-run it

Usage:
    python run_worker.py                     # live mode, 60s interval
    python run_worker.py --dry-run           # tick+promote but never call pipeline
    python run_worker.py --once              # one iteration then exit
    python run_worker.py --interval 30       # 30s between iterations
    python run_worker.py --channel-id foo    # restrict pipeline runs to channel 'foo'

Exit codes:
    0 — clean shutdown (SIGINT, SIGTERM, or --once)
    1 — startup failure (config, DB, or import error)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_DB    = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_DEFAULT_INTERVAL = 60  # seconds between iterations


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("worker")


# ---------------------------------------------------------------------------
# Shutdown flag — set by SIGINT / SIGTERM
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _shutdown_requested
    signame = signal.Signals(signum).name
    log.info("shutdown_signal received=%s — finishing current iteration", signame)
    _shutdown_requested = True


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Single iteration
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def run_iteration(
    worker_id: str,
    db_path: Path,
    dry_run: bool,
    channel_id: str | None,
    config,
) -> dict:
    """
    Execute one scheduler → consume → poll cycle.

    Returns a summary dict with counts from each step.
    """
    from hypogeum.scheduler import tick
    from hypogeum.queue import consume_scheduled_runs, poll

    now = _now_utc()

    # Step 1: tick — fire any due schedules
    sr_ids = tick(db_path=db_path, now=now)
    if sr_ids:
        log.info("tick scheduled_runs_created=%d ids=%s", len(sr_ids), sr_ids)
    else:
        log.debug("tick no_due_schedules")

    # Step 2: consume — promote pending scheduled_runs → run_queue
    queue_ids = consume_scheduled_runs(db_path=db_path)
    if queue_ids:
        log.info("consume queue_items_created=%d ids=%s", len(queue_ids), queue_ids)
    else:
        log.debug("consume nothing_pending")

    # Step 3: poll — claim one item from run_queue and execute
    poll_result = poll(
        worker_id=worker_id,
        db_path=db_path,
        dry_run=dry_run,
        max_iterations=1,
        sleep_interval=0.0,
        config=config,
    )

    proc = next((r for r in poll_result.results if r.status != "empty"), None)
    if proc:
        log.info(
            "poll status=%s queue_id=%s channel=%s dry_run=%s%s",
            proc.status,
            proc.queue_id,
            proc.channel_id or "(factory)",
            dry_run,
            f" error={proc.error_message[:120]}" if proc.error_message else "",
        )
    else:
        log.debug("poll queue_empty")

    return {
        "scheduled_runs_created": len(sr_ids),
        "queue_items_created":    len(queue_ids),
        "poll_status":            proc.status if proc else "empty",
        "poll_stop_reason":       poll_result.stop_reason,
        "dry_run":                dry_run,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_worker(
    db_path: Path,
    dry_run: bool,
    interval: float,
    once: bool,
    channel_id: str | None,
) -> int:
    """
    Run the worker loop. Returns exit code.
    """
    try:
        from hypogeum.config import Config
        config = Config()
    except Exception as exc:
        log.error("startup config_error=%s", exc)
        return 1

    try:
        from hypogeum.db import initialize_schema
        initialize_schema(db_path)
    except Exception as exc:
        log.error("startup db_error=%s", exc)
        return 1

    worker_id = str(uuid.uuid4())
    mode = "dry-run" if dry_run else "live"
    log.info(
        "worker_started worker_id=%s mode=%s interval=%ss db=%s channel=%s",
        worker_id, mode, interval, db_path, channel_id or "all",
    )

    iteration = 0
    while not _shutdown_requested:
        iteration += 1
        log.info("iteration=%d", iteration)

        try:
            result = run_iteration(
                worker_id=worker_id,
                db_path=db_path,
                dry_run=dry_run,
                channel_id=channel_id,
                config=config,
            )
            log.info(
                "iteration=%d done sr_created=%d q_created=%d poll=%s",
                iteration,
                result["scheduled_runs_created"],
                result["queue_items_created"],
                result["poll_status"],
            )
        except Exception as exc:
            log.error("iteration=%d error=%s", iteration, exc, exc_info=True)

        if once or _shutdown_requested:
            break

        log.debug("sleeping interval=%ss", interval)
        # Sleep in small increments so shutdown signal is noticed promptly
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not _shutdown_requested:
            time.sleep(min(1.0, deadline - time.monotonic()))

    log.info(
        "worker_stopped iterations=%d reason=%s",
        iteration,
        "signal" if _shutdown_requested else "once",
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_worker",
        description="Factory OS autonomous worker — scheduler → queue → pipeline loop",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Tick and promote schedules but never call run_pipeline. "
             "Items are marked completed immediately.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=_DEFAULT_INTERVAL,
        metavar="SECONDS",
        help=f"Seconds to sleep between iterations (default: {_DEFAULT_INTERVAL}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run exactly one iteration then exit.",
    )
    parser.add_argument(
        "--channel-id",
        metavar="CHANNEL",
        default=None,
        help="Restrict pipeline runs to this channel ID (optional).",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        help=f"Override database path (default: {_DEFAULT_DB}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db_path) if args.db_path else _DEFAULT_DB
    return run_worker(
        db_path=db_path,
        dry_run=args.dry_run,
        interval=args.interval,
        once=args.once,
        channel_id=args.channel_id,
    )


if __name__ == "__main__":
    sys.exit(main())
