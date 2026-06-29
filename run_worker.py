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

# Worker control DB helpers — imported lazily to avoid startup failures
_WC_AVAILABLE = False

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_DB    = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_DEFAULT_INTERVAL = 60  # seconds between iterations

# Analytics sync runs every this many iterations (default 60 = hourly at 60s interval).
# Override with env var ANALYTICS_SYNC_EVERY_N_ITERATIONS.
import os as _os
_ANALYTICS_SYNC_EVERY_N = int(_os.environ.get("ANALYTICS_SYNC_EVERY_N_ITERATIONS", "60"))


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


def _wc_set(key: str, value: str, db_path: Path) -> None:
    """Write a worker_control value. Silently swallows all errors."""
    try:
        from hypogeum.db import set_worker_control
        set_worker_control(key, value, db_path=db_path)
    except Exception:
        pass


def _wc_get(key: str, default: str, db_path: Path) -> str:
    """Read a worker_control value. Returns default on any error."""
    try:
        from hypogeum.db import get_worker_control
        return get_worker_control(key, default=default, db_path=db_path)
    except Exception:
        return default


def _sync_analytics_background(db_path: Path, config) -> None:
    """
    Background analytics sync — runs automatically, non-blocking, best-effort.

    YouTube: syncs if OAuth credentials are configured (source='api').
    TikTok / Instagram / Facebook: logged as skipped (connectors not implemented).
    """
    try:
        yt_ok = bool(
            getattr(config, "YOUTUBE_REFRESH_TOKEN", None)
            and getattr(config, "YOUTUBE_CLIENT_ID", None)
            and getattr(config, "YOUTUBE_CLIENT_SECRET", None)
        )
        if not yt_ok:
            log.debug("analytics_sync_skip reason=youtube_not_connected")
            return
        from hypogeum.youtube_analytics import sync_youtube_analytics
        result = sync_youtube_analytics(config, db_path=db_path, window_days=90)
        log.info(
            "analytics_sync_complete platform=youtube synced=%d skipped=%d",
            result.get("synced_count", 0),
            result.get("skipped_count", 0),
        )
    except Exception as exc:
        log.error("analytics_sync_error error=%s", exc)


def _check_and_refill_queue(
    db_path: Path,
    threshold: int,
    auto_approve: bool,
) -> dict:
    """
    Inspect topic queue depth across all active channels.

    For each active channel: if the number of queued topics is below
    `threshold`, run a discovery cycle to generate fresh candidates.
    If `auto_approve` is True, the top-scored candidate is automatically
    approved and enqueued, closing the loop without operator input.

    Duplicate-topic errors from enqueue_topic() are handled gracefully —
    treated as a successful enqueue of an already-queued topic.

    Returns:
        {
          "channels_checked": int,
          "channels_refilled": int,
          "cycles_run": list[str],     # cycle_ids
          "topics_enqueued": list[str] # topic texts (only when auto_approve)
        }
    """
    from hypogeum.channel_manager import list_channels
    from hypogeum.topic_discovery import run_discovery_cycle, approve_candidate
    from hypogeum.db import get_connection

    result: dict = {
        "channels_checked": 0,
        "channels_refilled": 0,
        "cycles_run": [],
        "topics_enqueued": [],
    }

    try:
        channels = list_channels(db_path=db_path)
    except Exception as exc:
        log.error("discovery_channel_list_error error=%s", exc)
        return result

    for ch in channels:
        channel_id = ch.channel_id
        result["channels_checked"] += 1

        # ── Queue depth check ─────────────────────────────────────────────
        try:
            conn = get_connection(db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM episode_topics "
                    "WHERE channel_id = ? AND status = 'queued'",
                    (channel_id,),
                ).fetchone()
                depth = row[0] if row else 0
            finally:
                conn.close()
        except Exception as exc:
            log.error("discovery_queue_depth_error channel=%s error=%s", channel_id, exc)
            continue

        if depth >= threshold:
            log.debug(
                "discovery_queue_sufficient channel=%s depth=%d threshold=%d",
                channel_id, depth, threshold,
            )
            continue

        # ── Queue is below threshold — run discovery cycle ────────────────
        log.info(
            "discovery_triggered channel=%s depth=%d threshold=%d",
            channel_id, depth, threshold,
        )
        try:
            cycle = run_discovery_cycle(channel_id, db_path=db_path)
        except Exception as exc:
            log.error("discovery_cycle_error channel=%s error=%s", channel_id, exc)
            continue

        if not cycle.get("ok"):
            log.error("discovery_cycle_failed channel=%s", channel_id)
            continue

        cycle_id = cycle["cycle_id"]
        result["cycles_run"].append(cycle_id)
        result["channels_refilled"] += 1
        log.info(
            "discovery_cycle_complete channel=%s cycle_id=%s candidates=%d selected=%s",
            channel_id, cycle_id,
            cycle.get("candidate_count", 0),
            cycle.get("selected_candidate_id"),
        )

        # ── Auto-approve: enqueue top candidate without operator input ────
        if not auto_approve:
            log.info(
                "discovery_pending_approval channel=%s cycle_id=%s "
                "(set AUTO_DISCOVERY_AUTO_APPROVE=true to enqueue automatically)",
                channel_id, cycle_id,
            )
            continue

        selected_id = cycle.get("selected_candidate_id")
        if not selected_id:
            log.warning("discovery_no_selected_candidate channel=%s cycle_id=%s", channel_id, cycle_id)
            continue

        try:
            approve_result = approve_candidate(selected_id, db_path=db_path)
        except Exception as exc:
            log.error("discovery_approve_error channel=%s candidate=%s error=%s",
                      channel_id, selected_id, exc)
            continue

        if approve_result.get("ok"):
            topic_text = approve_result.get("topic_text", "")
            result["topics_enqueued"].append(topic_text)
            log.info(
                "discovery_auto_enqueued channel=%s topic=%r topic_id=%s",
                channel_id, topic_text, approve_result.get("topic_id"),
            )
        else:
            code = approve_result.get("code", "")
            if code == "duplicate":
                # Topic already in queue — not an error for autonomous operation.
                log.info(
                    "discovery_duplicate_skipped channel=%s candidate=%s",
                    channel_id, selected_id,
                )
            else:
                log.error(
                    "discovery_approve_failed channel=%s candidate=%s error=%s",
                    channel_id, selected_id, approve_result.get("error"),
                )

    return result


def run_iteration(
    worker_id: str,
    db_path: Path,
    dry_run: bool,
    channel_id: str | None,
    config,
    iteration: int = 1,
) -> dict:
    """
    Execute one scheduler → consume → poll cycle.

    Returns a summary dict with counts from each step.
    Queue-paused mode: tick and consume run normally, but poll is skipped.
    """
    from hypogeum.scheduler import tick
    from hypogeum.queue import consume_scheduled_runs, poll

    now = _now_utc()
    queue_paused = _wc_get("queue_paused", "0", db_path) == "1"

    # Step 0: autonomous queue refill — runs before tick() so newly enqueued
    # topics are visible to the scheduler in the same iteration.
    discovery_result: dict = {}
    if getattr(config, "AUTO_DISCOVERY_ENABLED", False):
        try:
            discovery_result = _check_and_refill_queue(
                db_path=db_path,
                threshold=getattr(config, "AUTO_DISCOVERY_THRESHOLD", 2),
                auto_approve=getattr(config, "AUTO_DISCOVERY_AUTO_APPROVE", False),
            )
            if discovery_result.get("channels_refilled"):
                log.info(
                    "discovery_summary refilled=%d cycles=%s enqueued=%s",
                    discovery_result["channels_refilled"],
                    discovery_result["cycles_run"],
                    discovery_result["topics_enqueued"],
                )
        except Exception as exc:
            log.error("discovery_error error=%s", exc, exc_info=True)

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

    # Step 3: poll — claim one item from run_queue and execute.
    # Skip poll entirely when the queue is paused by operator.
    if queue_paused:
        log.info("poll queue_paused skipping_claim")
        from hypogeum.queue import PollResult
        poll_result = PollResult()
        proc = None
    else:
        _wc_set("worker_status", "claiming", db_path)
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

    # Step 4: background analytics sync — every _ANALYTICS_SYNC_EVERY_N iterations
    analytics_synced = False
    if not dry_run and iteration % _ANALYTICS_SYNC_EVERY_N == 0:
        log.info("analytics_sync_triggered iteration=%d", iteration)
        _sync_analytics_background(db_path=db_path, config=config)
        analytics_synced = True

    return {
        "scheduled_runs_created": len(sr_ids),
        "queue_items_created":    len(queue_ids),
        "poll_status":            proc.status if proc else ("paused" if queue_paused else "empty"),
        "poll_stop_reason":       poll_result.stop_reason,
        "dry_run":                dry_run,
        "discovery":              discovery_result,
        "analytics_synced":       analytics_synced,
        "queue_paused":           queue_paused,
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

    # Register this worker in the control table so the dashboard can show status.
    # Do NOT reset stop_requested here — the dashboard may have set it before
    # the worker started, and the loop checks it first thing.
    _wc_set("worker_id",      worker_id,    db_path)
    _wc_set("worker_status",  "idle",       db_path)
    _wc_set("active_queue_id", "",          db_path)

    # Reconcile orphaned runs before the loop starts. Any run stuck as 'running'
    # from a previous worker crash is stale — the current worker does not own it.
    try:
        from hypogeum.orphan_runs import reconcile_orphaned_runs
        orphaned = reconcile_orphaned_runs(
            db_path=db_path,
            worker_process_alive=False,  # this worker hasn't claimed anything yet
            worker_db_status="idle",
        )
        if orphaned:
            log.info("startup_reconcile orphaned_count=%d run_ids=%s", len(orphaned), orphaned)
    except Exception as exc:
        log.warning("startup_reconcile_error error=%s", exc)

    iteration = 0
    stop_reason = "once"
    while not _shutdown_requested:
        iteration += 1
        log.info("iteration=%d", iteration)

        # Check dashboard stop_requested signal
        if _wc_get("stop_requested", "0", db_path) == "1":
            log.info("worker_stop_requested_by_dashboard")
            _wc_set("stop_requested", "0", db_path)
            stop_reason = "dashboard_stop"
            break

        _wc_set("worker_status", "claiming", db_path)
        try:
            result = run_iteration(
                worker_id=worker_id,
                db_path=db_path,
                dry_run=dry_run,
                channel_id=channel_id,
                config=config,
                iteration=iteration,
            )
            log.info(
                "iteration=%d done sr_created=%d q_created=%d poll=%s%s",
                iteration,
                result["scheduled_runs_created"],
                result["queue_items_created"],
                result["poll_status"],
                " [queue_paused]" if result.get("queue_paused") else "",
            )
        except Exception as exc:
            log.error("iteration=%d error=%s", iteration, exc, exc_info=True)
        finally:
            _wc_set("worker_status", "idle", db_path)

        if once or _shutdown_requested:
            if _shutdown_requested:
                stop_reason = "signal"
            break

        log.debug("sleeping interval=%ss", interval)
        # Sleep in small increments so shutdown signal is noticed promptly
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not _shutdown_requested:
            time.sleep(min(1.0, deadline - time.monotonic()))

    _wc_set("worker_status", "stopped", db_path)
    log.info(
        "worker_stopped iterations=%d reason=%s",
        iteration,
        stop_reason if not _shutdown_requested else "signal",
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
