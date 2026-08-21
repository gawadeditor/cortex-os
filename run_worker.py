#!/usr/bin/env python3
"""
run_worker.py — Factory OS Autonomous Worker

Drives the full scheduler → queue → pipeline execution loop.

Each iteration:
  1. scheduler.tick()                  — enqueue pending scheduled_run rows for due schedules
  2. consume_scheduled_runs()          — promote pending scheduled_runs to run_queue
  2.5. factory_director.check_run_clearance() — hard gate; a new run is only
       claimed when the factory is cleared (no OIS blocking signal, factory
       active, active credentials, monthly budget ceiling not exceeded).
       Single source of truth — never reimplemented here, only consulted.
  3. queue.poll(max_iterations=1, pipeline_executor=<gated executor>) —
     claim one queued item; run pipeline or dry-run it (skipped when the
     operator has paused the queue, or when step 2.5 is not cleared)

Every real, queue-claimed pipeline run — autonomous or manual — passes
through the SAME executor built by
hypogeum.worker_integration.build_gated_pipeline_executor(), wired into the
existing queue.poll()/process_next() call via the pipeline_executor
parameter (hypogeum/queue.py). There is no second, queue-agnostic execution
path: claim, topic resolution, complete/fail/release, and the
hypogeum.pipeline.run_pipeline() call itself all remain exactly where they
already were, in hypogeum/queue.py. The executor only decides, per claimed
item, whether that one call is authorized (ChannelOperationsProfile +
BackwardPlan gates, concurrency, publishing platform filtering) and records
what happened — see hypogeum/worker_integration.py's module docstring for
the full gate order.

Usage:
    python run_worker.py                     # live mode, 60s interval
    python run_worker.py --dry-run           # tick+promote but never call pipeline
    python run_worker.py --once              # one iteration then exit
    python run_worker.py --interval 30       # 30s between iterations
    python run_worker.py --channel-id foo    # restrict pipeline runs to channel 'foo'

Manual supervised invocation (same gated executor, same queue lifecycle —
not a second implementation): enqueues one run_queue item for the given
channel/episode, then runs exactly one gated poll() iteration.
    python run_worker.py --gated-episode-id ep_001 --channel-id foo \
        --topic "The Antikythera Mechanism"

Bounded autonomous run (durable claim cap — see hypogeum/queue.py's
claim_next_bounded() and hypogeum/schema.sql's "bounded_runs /
bounded_run_claims" section): provision once, then claim through the same
bounded_run_id on every (re)launch. --bounded-run-id runs through the SAME
scheduler -> queue -> pipeline path as an unbounded run (topic discovery,
scheduler-originated queue rows, fresh episode_id synthesis included) — it
is not limited to a pre-existing --gated-episode-id.
    python run_worker.py --provision-bounded-run br_001 \
        --max-episode-claims 1                      # publishing_allowed defaults False
    python run_worker.py --once --bounded-run-id br_001   # normal autonomous claim, capped

--bounded-run-id can also be combined with --gated-episode-id to run one
specific, already-known episode through a bounded allowance instead:
    python run_worker.py --gated-episode-id ep_001 --channel-id foo \
        --bounded-run-id br_001

Exit codes:
    0 — clean shutdown (SIGINT, SIGTERM, or --once), or a gated invocation's
        claimed item completed successfully
    1 — startup failure (config, DB, or import error), or a gated
        invocation was blocked/refused/deferred/failed (see the printed
        JSON attempt/ProcessResult for blocked_reason)
"""

from __future__ import annotations

import argparse
import json
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
_WORKER_PID_FILE = _PROJECT_ROOT / ".hypogeum-assets" / "worker.pid"
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
    assets_root: Path | None = None,
) -> dict:
    """
    Inspect topic queue depth across all active channels.

    For each active channel: first check the one-episode publish-ready
    buffer (hypogeum.buffer.compute_buffer_state) — a channel with a
    sufficient or excess buffer (>=1 publish-ready, unpublished,
    non-current-week complete episode) is skipped entirely for this
    iteration, regardless of topic-backlog depth. This is a DIFFERENT gate
    than the queue-depth check below: queue depth is about the topic
    *backlog* (raw ideas awaiting production), the buffer gate is about
    *finished, publish-ready episodes* already ahead of the current
    schedule — see hypogeum/buffer.py's own docstring. Only when the buffer
    reports "enqueue" (0 publish-ready episodes ahead) does the existing
    queue-depth-driven discovery/auto-approve logic below run at all.

    Otherwise unchanged: if the number of queued topics is below
    `threshold`, run a discovery cycle to generate fresh candidates. If
    `auto_approve` is True, the top-scored candidate is automatically
    approved and enqueued, closing the loop without operator input.

    Duplicate-topic errors from enqueue_topic() are handled gracefully —
    treated as a successful enqueue of an already-queued topic.

    Returns:
        {
          "channels_checked": int,
          "channels_refilled": int,
          "cycles_run": list[str],     # cycle_ids
          "topics_enqueued": list[str] # topic texts (only when auto_approve)
          "buffer_gated": list[str],   # channel_ids skipped this iteration by the buffer gate
          "buffer_states": {channel_id: {"action": ..., "buffer_count": ..., "reason": ...}}
        }
    """
    from hypogeum.channel_config import list_channel_profiles
    from hypogeum.topic_discovery import run_discovery_cycle, approve_candidate
    from hypogeum.db import get_connection
    from hypogeum.buffer import compute_buffer_state, gather_buffer_candidates

    effective_assets_root = Path(assets_root) if assets_root is not None else Path(db_path).parent

    result: dict = {
        "channels_checked": 0,
        "channels_refilled": 0,
        "cycles_run": [],
        "topics_enqueued": [],
        "buffer_gated": [],
        "buffer_states": {},
    }

    try:
        # hypogeum.channel_manager.list_channels() reads the `channels` table,
        # which is populated by an older, now-unused channel-creation path
        # (only hypogeum/dashboard.py, itself dead/unimported, ever wrote to
        # it) and has always been empty in real operation -- this refill loop
        # was silently checking zero channels on every run. channel_profiles
        # (hypogeum.channel_config) is what the live dashboard and pipeline
        # actually populate and read. status="active" matches this
        # function's own documented "across all active channels" contract.
        channels = list_channel_profiles(status="active", db_path=db_path)
    except Exception as exc:
        log.error("discovery_channel_list_error error=%s", exc)
        return result

    for ch in channels:
        channel_id = ch.channel_id
        result["channels_checked"] += 1

        # ── One-episode buffer gate (Step 0 discovery-refill gate ONLY —
        # tick()/consume_scheduled_runs()/poll() below are never touched by
        # this) ──────────────────────────────────────────────────────────
        try:
            candidates = gather_buffer_candidates(
                channel_id, db_path=db_path, assets_root=effective_assets_root,
            )
            buffer_state = compute_buffer_state(channel_id, candidates)
            result["buffer_states"][channel_id] = {
                "action": buffer_state.action,
                "buffer_count": buffer_state.buffer_count,
                "reason": buffer_state.reason,
            }
        except Exception as exc:
            # Fail closed, matching this codebase's pervasive ambiguity
            # policy (production_output_gate.py, the Zernio adapter, media
            # staging): an unknown buffer state must never be treated as
            # "buffer empty, safe to enqueue more work".
            log.error("buffer_gate_error channel=%s error=%s — gating discovery (fail closed)", channel_id, exc)
            result["buffer_gated"].append(channel_id)
            result["buffer_states"][channel_id] = {"action": "error", "buffer_count": None, "reason": str(exc)}
            continue

        if buffer_state.action != "enqueue":
            result["buffer_gated"].append(channel_id)
            log.info(
                "discovery_buffer_gated channel=%s action=%s buffer_count=%d reason=%s",
                channel_id, buffer_state.action, buffer_state.buffer_count, buffer_state.reason,
            )
            continue

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
    validation_mode: bool = False,
    bounded_run_id: str | None = None,
) -> dict:
    """
    Execute one scheduler → consume → poll cycle.

    validation_mode: narrow, explicit exemption from the monthly
        budget-ceiling clearance check ONLY (added 2026-07-18) — see
        hypogeum.factory_director.check_run_clearance()'s own docstring
        for the full contract. Defaults to False and is NEVER set by
        run_worker() (the actual autonomous daemon loop below) or by any
        CLI flag — only an explicit, one-off caller (e.g. a supervised
        validation script calling run_iteration() directly) can set it.

    bounded_run_id: Optional. None (default, every existing caller)
        preserves prior behavior exactly — Step 3's poll() claims via the
        plain unbounded claim_next() and the gated executor carries no
        bounded_run_id. When given, forwarded unchanged to both poll()
        (so Step 3 claims via claim_next_bounded() — same candidate set and
        priority/FIFO ordering as unbounded claiming, including a fresh
        scheduler-originated row whose episode_id starts NULL) and
        build_gated_pipeline_executor() (so publishing_allowed=0 on that
        bounded run is enforced at the real publishing boundary for this
        claim). Never silently falls back to unbounded claiming — a
        BoundedRunError surfaces as poll_status="empty" with the reason in
        the corresponding ProcessResult.error_message (see process_next()'s
        own docstring).

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

    # Step 2.5: factory clearance gate — hard block before claiming/starting
    # any queued item. hypogeum.factory_director.check_run_clearance() is
    # the single source of truth (OIS blocking signals, factory-active
    # status, active credentials, and monthly budget-ceiling overage) —
    # its logic is never duplicated here, only consulted.
    from hypogeum.factory_director import check_run_clearance
    clearance = check_run_clearance(
        db_path=db_path, channel_id=channel_id, validation_mode=validation_mode,
    )
    if clearance.validation_exemption:
        log.warning(
            "clearance_validation_exemption_applied reason=%r",
            clearance.validation_exemption,
        )

    # Step 3: poll — claim one item from run_queue and execute.
    # Skip poll entirely when the queue is paused by operator, or when the
    # factory is not cleared for a new run.
    if queue_paused or not clearance.cleared:
        if queue_paused:
            log.info("poll queue_paused skipping_claim")
        else:
            log.warning("poll not_cleared skipping_claim reason=%s", clearance.reason)
        from hypogeum.queue import PollResult
        poll_result = PollResult()
        proc = None
    else:
        _wc_set("worker_status", "claiming", db_path)
        from hypogeum.worker_integration import build_gated_pipeline_executor
        gated_executor = build_gated_pipeline_executor(db_path=db_path, bounded_run_id=bounded_run_id)
        poll_result = poll(
            worker_id=worker_id,
            db_path=db_path,
            dry_run=dry_run,
            max_iterations=1,
            sleep_interval=0.0,
            config=config,
            pipeline_executor=gated_executor,
            bounded_run_id=bounded_run_id,
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

    poll_status = proc.status if proc else (
        "paused" if queue_paused else ("not_cleared" if not clearance.cleared else "empty")
    )
    return {
        "scheduled_runs_created": len(sr_ids),
        "queue_items_created":    len(queue_ids),
        "poll_status":            poll_status,
        "poll_stop_reason":       poll_result.stop_reason,
        "dry_run":                dry_run,
        "discovery":              discovery_result,
        "analytics_synced":       analytics_synced,
        "queue_paused":           queue_paused,
        "factory_cleared":        clearance.cleared,
        "clearance_reason":       clearance.reason,
        "validation_exemption":   clearance.validation_exemption,
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
    bounded_run_id: str | None = None,
) -> int:
    """
    Run the worker loop. Returns exit code.

    bounded_run_id: Optional. None (default, every existing caller)
        preserves prior behavior exactly — unbounded claim_next() every
        iteration. When given, must already be provisioned (checked here
        via _validate_bounded_run_ready() before the loop even starts —
        fail closed with a clear error rather than looping silently on
        poll_status="empty"). Forwarded unchanged into every run_iteration()
        call, which forwards it into poll()/build_gated_pipeline_executor()
        — the SAME real scheduler → queue → pipeline path every autonomous
        run already takes; there is no second, bounded-only execution path.
        This applies to fresh scheduler-originated queue rows exactly like
        every other queued item: bounded claiming is never restricted to
        pre-existing --gated-episode-id items.
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

    if bounded_run_id is not None:
        reason = _validate_bounded_run_ready(bounded_run_id, db_path)
        if reason is not None:
            log.error("startup_bounded_run_invalid reason=%s", reason)
            print(reason, file=sys.stderr)
            return 1

    worker_id = str(uuid.uuid4())
    mode = "dry-run" if dry_run else "live"
    log.info(
        "worker_started worker_id=%s mode=%s interval=%ss db=%s channel=%s bounded_run_id=%s",
        worker_id, mode, interval, db_path, channel_id or "all", bounded_run_id or "(unbounded)",
    )

    # Startup verification (Phase 3 Production Hardening, P0.5): run the
    # preflight checks that already existed as manual CLI tools
    # (hypogeum/setup_preflight.py) automatically now. Never refuses to
    # start — the scheduler/queue loop has value even with a blocker
    # present — but alerts loudly via the same OIS signal substrate P0.3/
    # P0.4 use, so an operator learns about it immediately instead of via
    # a string of failed runs.
    try:
        from hypogeum.startup_verification import verify_startup
        preflight = verify_startup(db_path=db_path, config=config)
        if not preflight["ready"]:
            log.warning(
                "startup_preflight_blockers count=%d names=%s",
                preflight["blocker_count"],
                [b["name"] for b in preflight["blockers"]],
            )
    except Exception as exc:
        log.warning("startup_preflight_error error=%s", exc)

    # Write PID file so the dashboard can detect this worker regardless of whether
    # it was started via the dashboard UI or directly from the CLI. Without this,
    # _get_worker_pid() returns None for CLI workers, causing the dashboard's
    # orphan reconciliation to falsely mark active runs as orphaned.
    try:
        _WORKER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WORKER_PID_FILE.write_text(str(_os.getpid()))
    except Exception as exc:
        log.warning("worker_pid_file_write_failed error=%s", exc)

    # Remove PID file when this process exits for any reason (normal or signal).
    import atexit as _atexit
    _pid_str = str(_os.getpid())
    def _remove_pid_file():
        try:
            if _WORKER_PID_FILE.exists() and _WORKER_PID_FILE.read_text().strip() == _pid_str:
                _WORKER_PID_FILE.unlink()
        except Exception:
            pass
    _atexit.register(_remove_pid_file)

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

        # Self-healing sweep (Phase 3 Production Hardening, P0.4): orphan
        # reconciliation previously only ran at startup/dashboard-page-load
        # trigger moments — running it here, every iteration, before this
        # worker claims anything (worker_status is still 'idle' at this
        # point), closes the gap BURN_IN_READINESS_AUDIT.md found. Safe and
        # cheap: reconcile_orphaned_runs() short-circuits to a no-op
        # whenever nothing is actually orphaned.
        try:
            from hypogeum.self_healing import periodic_check
            healing_result = periodic_check(
                db_path=db_path, config=config,
                worker_process_alive=True, worker_db_status="idle",
            )
            if healing_result["orphaned_run_ids"] or healing_result["retry_exhausted_alerted"]:
                log.info(
                    "self_healing_sweep orphaned=%s retry_exhausted=%s",
                    healing_result["orphaned_run_ids"],
                    healing_result["retry_exhausted_alerted"],
                )
        except Exception as exc:
            log.warning("self_healing_sweep_error error=%s", exc)

        _wc_set("worker_status", "claiming", db_path)
        try:
            result = run_iteration(
                worker_id=worker_id,
                db_path=db_path,
                dry_run=dry_run,
                channel_id=channel_id,
                config=config,
                iteration=iteration,
                bounded_run_id=bounded_run_id,
            )
            log.info(
                "iteration=%d done sr_created=%d q_created=%d poll=%s%s%s",
                iteration,
                result["scheduled_runs_created"],
                result["queue_items_created"],
                result["poll_status"],
                " [queue_paused]" if result.get("queue_paused") else "",
                f" [not_cleared: {result['clearance_reason']}]" if not result.get("factory_cleared") else "",
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
    parser.add_argument(
        "--gated-episode-id",
        metavar="EPISODE_ID",
        default=None,
        help="Enqueue and run exactly one gated episode through the real "
             "queue lifecycle (claim -> gates -> pipeline -> complete/fail), "
             "print the resulting ProcessResult as JSON, and exit. Requires "
             "--channel-id. Does not enter the daemon loop.",
    )
    parser.add_argument(
        "--topic",
        metavar="TEXT",
        default=None,
        help="Explicit topic text for --gated-episode-id. If omitted, the topic "
             "is claimed from the channel's existing topic backlog.",
    )
    parser.add_argument(
        "--provision-bounded-run",
        metavar="BOUNDED_RUN_ID",
        default=None,
        help="Explicitly provision a bounded run (see hypogeum.queue."
             "provision_bounded_run()), print the resulting bounded_runs row "
             "as JSON, and exit. Requires --max-episode-claims. Idempotent "
             "for a restart of the same bounded_run_id with identical "
             "parameters. Does not claim anything or enter the daemon loop.",
    )
    parser.add_argument(
        "--max-episode-claims",
        type=int,
        default=None,
        metavar="N",
        help="Durable claim cap for --provision-bounded-run. Must be > 0.",
    )
    parser.add_argument(
        "--publishing-allowed",
        action="store_true",
        default=False,
        help="Authorize publishing for --provision-bounded-run. Omit to "
             "provision a bounded run that can never reach a publishing "
             "boundary (the default).",
    )
    parser.add_argument(
        "--bounded-run-id",
        metavar="BOUNDED_RUN_ID",
        default=None,
        help="Claim through an already-provisioned bounded run "
             "(--provision-bounded-run) instead of unbounded claiming. Used "
             "alone, applies to the normal autonomous daemon loop (same "
             "scheduler -> queue -> pipeline path, including fresh "
             "scheduler-originated / topic-backlog episodes). Combine with "
             "--gated-episode-id to instead run one specific, already-known "
             "episode through the bounded allowance. Fails closed before "
             "any claiming if the bounded_run_id is unknown, not active, or "
             "already exhausted — never falls back to unbounded claiming.",
    )
    return parser.parse_args(argv)


def _validate_bounded_run_ready(bounded_run_id: str, db_path: Path) -> str | None:
    """
    Read-only pre-flight check for an already-provisioned bounded run.

    Returns None when the bounded run exists and is 'active'; otherwise a
    human-readable reason ("unknown bounded_run_id", "not active
    (status=...)"). Used by every bounded CLI entry point (--gated-episode-id
    + --bounded-run-id, and the plain autonomous daemon loop) to fail closed
    *before* starting any claiming, rather than a daemon loop silently
    reporting "empty" every iteration with no diagnostic. The real,
    authoritative enforcement remains inside
    hypogeum.queue.claim_next_bounded() regardless of this check — this
    function never provisions, claims, or mutates anything.
    """
    from hypogeum.db import get_bounded_run, get_connection

    conn = get_connection(db_path)
    try:
        bounded_run = get_bounded_run(conn, bounded_run_id)
    finally:
        conn.close()
    if bounded_run is None:
        return (
            f"unknown bounded_run_id: {bounded_run_id!r} — provision it first "
            f"with --provision-bounded-run"
        )
    if bounded_run["status"] != "active":
        return f"bounded_run_id {bounded_run_id!r} is not active (status={bounded_run['status']!r})"
    return None


def _run_gated_cli(
    channel_id: str, episode_id: str, topic: str | None, db_path: Path,
    bounded_run_id: str | None = None,
) -> int:
    """
    Enqueues one run_queue item for (channel_id, episode_id) and runs a
    single gated poll() iteration against it — the exact same queue
    lifecycle (claim/topic-resolve/complete/fail/release) and the exact
    same gated executor the daemon loop uses, not a second implementation.
    Returns a process exit code: 0 only for a genuinely completed run.

    bounded_run_id: Optional. None (default) preserves prior behavior
        exactly — unbounded claim_next(). When given, must already be
        provisioned via --provision-bounded-run (checked here via
        _validate_bounded_run_ready() for a fast, clear CLI error; the real
        enforcement is inside hypogeum.queue.claim_next_bounded()
        regardless) — this function never provisions one itself.
    """
    from hypogeum.config import Config
    from hypogeum.db import initialize_schema
    from hypogeum.queue import enqueue, poll
    from hypogeum.worker_integration import build_gated_pipeline_executor

    try:
        initialize_schema(db_path)
        config = Config()
    except Exception as exc:
        print(f"startup error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if bounded_run_id is not None:
        reason = _validate_bounded_run_ready(bounded_run_id, db_path)
        if reason is not None:
            print(reason, file=sys.stderr)
            return 1

    queue_id = enqueue(
        db_path=db_path, channel_id=channel_id, episode_id=episode_id,
        priority=-100, source="manual",
    )
    topic_resolver = (lambda _claimed_row: topic) if topic else None
    executor = build_gated_pipeline_executor(db_path=db_path, bounded_run_id=bounded_run_id)

    result = poll(
        worker_id=str(uuid.uuid4()), db_path=db_path, dry_run=False,
        max_iterations=1, config=config, topic_resolver=topic_resolver,
        pipeline_executor=executor, bounded_run_id=bounded_run_id,
    )
    proc = next((r for r in result.results if r.status != "empty"), None)
    output = proc.__dict__ if proc is not None else {"status": "empty", "queue_id": queue_id}
    print(json.dumps(output, indent=2, ensure_ascii=False, default=str))

    return 0 if proc is not None and proc.status == "completed" else 1


def _run_provision_bounded_cli(
    bounded_run_id: str, max_episode_claims: int | None, publishing_allowed: bool,
    db_path: Path,
) -> int:
    """
    Explicitly provisions a bounded run (hypogeum.queue.provision_bounded_run)
    and prints the resulting bounded_runs row as JSON. Makes no run_queue
    claim and calls no pipeline/provider/publishing code.
    """
    from hypogeum.db import BoundedRunError, initialize_schema
    from hypogeum.queue import provision_bounded_run

    if not max_episode_claims:
        print("--provision-bounded-run requires --max-episode-claims", file=sys.stderr)
        return 1

    try:
        initialize_schema(db_path)
        row = provision_bounded_run(
            bounded_run_id=bounded_run_id,
            max_episode_claims=max_episode_claims,
            publishing_allowed=publishing_allowed,
            db_path=db_path,
        )
    except BoundedRunError as exc:
        print(f"provision error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(row, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db_path) if args.db_path else _DEFAULT_DB

    # --publishing-allowed / --max-episode-claims are provisioning-only
    # parameters -- fail closed if either is supplied without
    # --provision-bounded-run, rather than silently accepting and ignoring
    # a flag whose name implies it did something.
    if not args.provision_bounded_run:
        if args.publishing_allowed:
            print("--publishing-allowed is only valid together with --provision-bounded-run",
                  file=sys.stderr)
            return 1
        if args.max_episode_claims is not None:
            print("--max-episode-claims is only valid together with --provision-bounded-run",
                  file=sys.stderr)
            return 1

    if args.provision_bounded_run:
        return _run_provision_bounded_cli(
            bounded_run_id=args.provision_bounded_run,
            max_episode_claims=args.max_episode_claims,
            publishing_allowed=args.publishing_allowed,
            db_path=db_path,
        )

    if args.gated_episode_id:
        if not args.channel_id:
            print("--gated-episode-id requires --channel-id", file=sys.stderr)
            return 1
        return _run_gated_cli(
            channel_id=args.channel_id, episode_id=args.gated_episode_id,
            topic=args.topic, db_path=db_path,
            bounded_run_id=args.bounded_run_id,
        )

    # Normal autonomous daemon loop. --bounded-run-id does NOT require
    # --gated-episode-id -- an already-provisioned bounded run threads
    # straight through to run_worker()'s real scheduler -> queue -> pipeline
    # loop (see run_worker()'s own docstring), covering fresh
    # scheduler-originated / topic-backlog episodes, not just a pre-created
    # --gated-episode-id.
    return run_worker(
        db_path=db_path,
        dry_run=args.dry_run,
        interval=args.interval,
        once=args.once,
        channel_id=args.channel_id,
        bounded_run_id=args.bounded_run_id,
    )


if __name__ == "__main__":
    sys.exit(main())
