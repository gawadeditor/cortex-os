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
_HEALTH_CHECK_EVERY_N = int(_os.environ.get("HEALTH_CHECK_EVERY_N_ITERATIONS", "5"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.Formatter.converter = time.gmtime  # %(asctime)s in real UTC, not local
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


def is_worker_heartbeat_stale(db_path: Path, max_age_seconds: float = 300.0) -> bool:
    """
    True when worker_control.worker_heartbeat_at (see run_worker()'s own
    heartbeat write, Step-loop, 2026-08-23) is older than max_age_seconds,
    OR has never been recorded at all -- the "no evidence of liveness" case
    is treated as stale, never as "not stale because we can't tell", so a
    caller that never checks for None separately still fails safe.

    Never raises for a malformed/missing worker_control row -- returns
    True (stale) for anything it cannot parse, matching the same fail-
    closed-on-ambiguity discipline the rest of this module already uses
    for the singleton lock and per-channel operator_enabled gate.

    Pure read -- makes no database write, never touches the singleton
    lock or heartbeat itself.
    """
    raw = _wc_get("worker_heartbeat_at", "", db_path)
    if not raw:
        return True
    try:
        heartbeat_at = datetime.fromisoformat(raw)
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    try:
        age_seconds = (_now_utc() - heartbeat_at).total_seconds()
    except TypeError:
        return True
    return age_seconds > max_age_seconds


_worker_singleton_lock_registered = False


def _acquire_worker_singleton_lock() -> str | None:
    """
    Refuse to start a second, concurrent, mutation-capable process against
    the same DB. Returns None on success (lock acquired, this process's
    own PID now written to _WORKER_PID_FILE, atexit cleanup registered);
    a human-readable conflict reason otherwise (nothing written).

    2026-08-23, corrected same day: originally scoped to the long-running
    daemon only (an --once/--gated-episode-id/bounded one-shot was
    exempted, on the theory that such invocations were "designed to run
    alongside a daemon for a single supervised operation"). That was
    wrong -- a gated or one-shot invocation still claims from run_queue,
    still runs the real pipeline, still stages/publishes; it is exactly
    as mutation-capable as the daemon loop and must be covered by the
    SAME singleton lock. Every real run_worker.py entry point (the
    daemon's own run_worker(), and _run_gated_cli()) now calls this one
    shared function -- there is no second, narrower lock. The only things
    exempt from ever calling this at all are genuinely read-only
    diagnostic tools that never claim/write anything (e.g.
    scripts/schedule_projection_hypogeum.py), which simply never call it
    in the first place -- there is no in-process bypass flag.

    _WORKER_PID_FILE is the same fixed, hardcoded, cross-module-shared
    path run_dashboard.py and hypogeum/observability.py both
    independently read to detect "is a worker running" -- this function
    never changes its location, only adds the missing check-before-write
    it never had.

    Liveness check: os.kill(pid, 0) — raises ProcessLookupError for a
    dead PID (stale lock file from a crash — safely overwritten,
    logged), PermissionError for a live PID owned by another user (still
    a real conflict — refused), or succeeds silently for a live PID
    owned by this same user (a real conflict — refused).
    """
    global _worker_singleton_lock_registered

    try:
        if _WORKER_PID_FILE.exists():
            existing_pid_str = _WORKER_PID_FILE.read_text().strip()
            if existing_pid_str and int(existing_pid_str) == _os.getpid():
                # Our OWN pid, pre-written by run_dashboard.py's
                # /api/worker/start: it Popen's this process, then writes
                # proc.pid to this shared file before our (slow) imports
                # reach here. Not a second process -- fall through and
                # re-write it + register the atexit cleanup as normal.
                log.info(
                    "worker_pid_file_is_self existing_pid=%s — dashboard pre-registered this process",
                    existing_pid_str,
                )
            elif existing_pid_str:
                existing_pid = int(existing_pid_str)
                try:
                    _os.kill(existing_pid, 0)
                except ProcessLookupError:
                    log.warning(
                        "worker_pid_file_stale existing_pid=%d — no such process, overwriting",
                        existing_pid,
                    )
                except PermissionError:
                    return (
                        f"existing_pid={existing_pid} is alive (owned by another user) — "
                        f"refusing to start a second mutation-capable process against the same DB"
                    )
                else:
                    return (
                        f"existing_pid={existing_pid} is alive — refusing to start a second "
                        f"mutation-capable process against the same DB"
                    )
    except (ValueError, OSError) as exc:
        log.warning("worker_pid_file_check_error error=%s — proceeding", exc)

    try:
        _WORKER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WORKER_PID_FILE.write_text(str(_os.getpid()))
    except Exception as exc:
        log.warning("worker_pid_file_write_failed error=%s", exc)
        return None  # best-effort -- a write failure alone never blocks the caller

    if not _worker_singleton_lock_registered:
        import atexit as _atexit
        _atexit.register(_release_worker_singleton_lock)
        _worker_singleton_lock_registered = True

    return None


def _release_worker_singleton_lock() -> None:
    """Removes _WORKER_PID_FILE only if it still names THIS process's own
    PID (never another process's, e.g. one that acquired the lock after
    this process's own stale registration somehow survived). Safe to call
    multiple times (idempotent) and safe to call when the lock was never
    acquired (no-op)."""
    try:
        if _WORKER_PID_FILE.exists() and _WORKER_PID_FILE.read_text().strip() == str(_os.getpid()):
            _WORKER_PID_FILE.unlink()
    except Exception:
        pass


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


def _run_learning_cycle(db_path: Path) -> dict:
    """
    Close the self-learning loop: for every episode that has real
    (source='api') platform analytics, run the learning engine's
    idempotent extract_insights() -> generate_actions() pass so fresh
    insights and pending learning_actions are always available to the
    operator (and, in future, to discovery).

    Best-effort and non-blocking — a failure for one episode is logged and
    the sweep continues. Both learning-engine calls are INSERT-OR-IGNORE
    idempotent, so re-running every analytics-sync cycle is safe and cheap.
    """
    summary = {"episodes": 0, "insights": 0, "actions": 0, "cadence_recs": 0}
    try:
        from hypogeum.analytics_store import list_analytics
        from hypogeum.learning_engine import (
            extract_insights, generate_actions, generate_cadence_recommendation,
        )

        records = list_analytics(db_path=db_path, limit=500)
        # F8 data-integrity contract: only real API analytics drive learning.
        episode_ids = {
            r["episode_id"] for r in records
            if r.get("episode_id") and (r.get("source") or "") == "api"
        }
        for ep in sorted(episode_ids):
            try:
                ins = extract_insights(ep, db_path=db_path)
                acts = generate_actions(ep, db_path=db_path)
                summary["episodes"] += 1
                summary["insights"] += len(ins or [])
                summary["actions"] += len(acts or [])
            except Exception as exc:
                log.error("learning_cycle_episode_error episode=%s error=%s", ep, exc)

        # Per-channel content_volume (weekly-cadence) recommendation.
        try:
            from hypogeum.channel_config import list_channel_profiles
            for cp in list_channel_profiles(db_path=db_path):
                cid = getattr(cp, "channel_id", None)
                if not cid:
                    continue
                if generate_cadence_recommendation(cid, db_path=db_path) is not None:
                    summary["cadence_recs"] += 1
        except Exception as exc:
            log.error("learning_cycle_cadence_error error=%s", exc)

        if summary["episodes"] or summary["cadence_recs"]:
            log.info(
                "learning_cycle_complete episodes=%d insights=%d pending_actions=%d cadence_recs=%d",
                summary["episodes"], summary["insights"], summary["actions"], summary["cadence_recs"],
            )
    except Exception as exc:
        log.error("learning_cycle_error error=%s", exc)
    return summary


def _resolve_bundle_definitions_for_channel(channel_id: str, db_path: Path | None = None) -> list:
    """
    Real Channel-Package-driven bundle compiler (2026-08-23 — closes the
    gap this function's own docstring previously documented, and
    hypogeum/buffer.py's SCHEMA GAP note references). Compiles
    ChannelOperationsProfile.deliverable_bundle_definitions — a generic,
    channel-declared list of {"bundle_id": str, "target_buffer_count": int}
    entries (see that field's own docstring in hypogeum/channel_config.py)
    — into hypogeum.buffer.DeliverableBundleDefinition objects. Factory
    Core assigns no meaning to bundle_id or how many entries a channel
    declares; hypogeum/buffer.py itself remains fully channel-agnostic and
    is never touched by this function's existence.

    A channel with no ChannelOperationsProfile, or with an empty/absent
    deliverable_bundle_definitions, returns [] — the buffer gate in
    _check_and_refill_queue() is then skipped entirely for it (falls
    through to the existing queue-depth-only refill logic, unaffected),
    the same fail-closed-by-omission behavior this function always had:
    never guess a bundle shape onto a channel that hasn't declared one.
    Malformed entries (missing bundle_id or a non-positive
    target_buffer_count) are skipped individually, never silently
    defaulted and never crashing the whole compilation.
    """
    from hypogeum.buffer import DeliverableBundleDefinition
    from hypogeum.channel_config import get_channel_operations_profile

    try:
        profile = get_channel_operations_profile(channel_id, db_path=db_path)
    except RuntimeError as exc:
        log.error("bundle_definitions_profile_read_error channel=%s error=%s", channel_id, exc)
        return []
    if profile is None:
        return []

    definitions = []
    for entry in (profile.deliverable_bundle_definitions or []):
        bundle_id = entry.get("bundle_id") if isinstance(entry, dict) else None
        target = entry.get("target_buffer_count") if isinstance(entry, dict) else None
        if not bundle_id or not isinstance(target, int) or isinstance(target, bool) or target <= 0:
            log.warning(
                "bundle_definitions_invalid_entry channel=%s entry=%r — skipped",
                channel_id, entry,
            )
            continue
        definitions.append(DeliverableBundleDefinition(
            channel_id=channel_id, bundle_id=bundle_id, target_buffer_count=target,
        ))
    return definitions


def _process_channel_publishing(db_path: Path, assets_root: Path) -> dict:
    """
    Autonomous publishing step: for each active channel with a resolvable
    bundle definition, find its not-yet-published, complete bundles and
    submit them through hypogeum.publishing.zernio_channel_release_trigger.
    process_channel_ready_bundles() — the SAME function an operator could
    call manually, now invoked automatically on every worker iteration
    (gated by Config.AUTO_ZERNIO_PUBLISHING_ENABLED — see run_iteration()).

    Fail-closed contract:
      - Absent configuration (no ChannelOperationsProfile, no enabled
        platforms, publishing_enabled=False) or missing provider bindings
        are handled INSIDE process_channel_ready_bundles()/
        publish_channel_release() — every such case returns a
        "blocked_*" result per item, never an exception, so this function
        never needs to distinguish "not configured" from "configured and
        working" to stay safe.
      - An UNEXPECTED exception for one channel (a real bug, a transient
        I/O error, etc.) is caught and logged HERE, per channel, so one
        channel's failure can never stop another channel's processing or
        crash the worker loop. This is the one thing
        process_channel_ready_bundles() itself cannot guarantee on its
        own, since it has no outer try/except of its own by design (it is
        also called directly by operators, who want a raised exception to
        actually surface).

    No Hypogeum-specific branch: every channel returned by
    list_channel_profiles(status="active") is treated identically;
    _resolve_bundle_definitions_for_channel() (unchanged by this function)
    is the one, already-existing, documented seam where a real
    Channel-Package compiler will eventually replace the current
    Hypogeum-only stand-in — this function never special-cases 'hypogeum'
    itself, and a channel with no resolvable bundle definition is simply
    skipped (same fail-closed-by-omission behavior _check_and_refill_queue()
    already uses for its own buffer gate).

    Idempotency across repeated calls (every worker iteration, or a fresh
    call after a worker restart) is inherited entirely from
    process_channel_ready_bundles()'s own contract — this function adds no
    state or tracking of its own.
    """
    from hypogeum.channel_config import list_channel_profiles
    from hypogeum.publishing.zernio_channel_release_trigger import process_channel_ready_bundles

    result: dict = {"channels_checked": 0, "channels_with_activity": 0, "results_by_channel": {}}

    try:
        channels = list_channel_profiles(status="active", db_path=db_path)
    except Exception as exc:
        log.error("publishing_channel_list_error error=%s", exc)
        return result

    for ch in channels:
        channel_id = ch.channel_id
        result["channels_checked"] += 1

        # Per-channel operator pause -- same operator_enabled check as
        # _check_and_refill_queue()'s own (see that call site's comment
        # for the full rationale); an operator-disabled channel's already-
        # complete deliverables are never auto-published either.
        #
        # 2026-08-23 (autonomy safety semantics correction): fails CLOSED
        # on a missing/unreadable profile -- a channel with no
        # ChannelOperationsProfile at all, or one this read couldn't
        # parse, must never be treated as autonomously enabled for a
        # mutating/paid action (publishing spends real provider calls and
        # makes real external posts). This mirrors hypogeum.worker_
        # integration.load_operations_profile_or_fail()'s own contract
        # for production execution -- staging/publishing get the same
        # fail-closed guarantee here, at the autonomous sweep's own entry
        # point, rather than relying solely on _resolve_bundle_definitions_
        # for_channel()'s downstream empty-list fallback to accidentally
        # produce the same result.
        from hypogeum.channel_config import get_channel_operations_profile
        try:
            ops_profile = get_channel_operations_profile(channel_id, db_path=db_path)
        except RuntimeError as exc:
            log.error("publishing_operator_enabled_check_error channel=%s error=%s", channel_id, exc)
            ops_profile = None
        if ops_profile is None or not ops_profile.operator_enabled:
            log.info(
                "publishing_operator_disabled_or_no_profile channel=%s has_profile=%s — skipping",
                channel_id, ops_profile is not None,
            )
            continue

        bundle_definitions = _resolve_bundle_definitions_for_channel(channel_id, db_path=db_path)
        if not bundle_definitions:
            continue

        # A shorts-only channel (videos_per_week == 0, shorts_per_week >= 1)
        # publishes each freshly produced Short immediately — there is no
        # weekly bundle drop to schedule against, and compute_next_publish_slot()
        # (weekly-only) cannot express a daily release. Any mixed/long-form
        # channel keeps the scheduled path (publish_now=False).
        _cad = (ops_profile.production_cadence or {})
        _spw = _cad.get("shorts_per_week")
        publish_immediately = (
            _cad.get("videos_per_week") == 0 and isinstance(_spw, int) and _spw >= 1
        )

        try:
            channel_results = []
            for bundle_def in bundle_definitions:
                channel_results.extend(process_channel_ready_bundles(
                    channel_id, bundle_def, db_path=db_path, assets_root=assets_root,
                    publish_now=publish_immediately,
                ))
        except Exception as exc:
            log.error("publishing_channel_error channel=%s error=%s", channel_id, exc, exc_info=True)
            continue

        if channel_results:
            result["channels_with_activity"] += 1
            result["results_by_channel"][channel_id] = [
                {"item_id": r.item_id, "platform": r.platform, "status": r.result.status}
                for r in channel_results
            ]
            for r in channel_results:
                log.info(
                    "publishing_attempt channel=%s item=%s platform=%s status=%s",
                    channel_id, r.item_id, r.platform, r.result.status,
                )

    return result


def _refresh_scheduling_evidence_for_all_channels(db_path: Path) -> dict:
    """
    2026-08-23 (autonomous weekly runtime, Phase 3 requirement #1/#8):
    "the daemon must refresh scheduling evidence automatically before
    compiling future schedules" and "when evidence becomes sufficient,
    the periodic analytics sync must allow later cycles to move
    automatically to analytics mode". Runs immediately after the periodic
    analytics sync (Step 4, same iteration) so freshly-synced
    platform_analytics rows feed straight into the next schedule
    compilation without a separate manual trigger -- see
    hypogeum.publishing.evidence_based_scheduling.
    refresh_analytics_scheduling_policy()'s own docstring for exactly
    what it writes and why a periodic (not per-publish) refresh is
    correct here.

    No Hypogeum-specific branch: every channel returned by
    list_channel_profiles(status="active") is treated identically, and is
    skipped entirely (silently, not an error) when it has no
    analytics_scheduling_policy configured at all -- see that function's
    own "never raises for missing configuration" contract. An UNEXPECTED
    exception for one channel is caught and logged HERE, per channel, so
    one channel's failure can never stop another channel's refresh or
    crash the worker loop -- the same isolation pattern
    _process_channel_publishing()/_check_and_refill_queue() already use.
    """
    from hypogeum.channel_config import list_channel_profiles
    from hypogeum.publishing.evidence_based_scheduling import refresh_analytics_scheduling_policy

    result: dict = {"channels_checked": 0, "channels_updated": []}

    try:
        channels = list_channel_profiles(status="active", db_path=db_path)
    except Exception as exc:
        log.error("scheduling_evidence_channel_list_error error=%s", exc)
        return result

    for ch in channels:
        channel_id = ch.channel_id
        result["channels_checked"] += 1
        try:
            refresh_result = refresh_analytics_scheduling_policy(channel_id, db_path=db_path)
        except Exception as exc:
            log.error("scheduling_evidence_refresh_error channel=%s error=%s", channel_id, exc, exc_info=True)
            continue
        if refresh_result.deliverables_updated:
            result["channels_updated"].append(channel_id)
            log.info(
                "scheduling_evidence_refreshed channel=%s deliverables_updated=%s",
                channel_id, refresh_result.deliverables_updated,
            )

    return result


def _topic_text_for_id(topic_id: str | None, db_path: Path) -> str | None:
    """Human topic text for an episode_topics.topic_id, or None."""
    if not topic_id:
        return None
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT topic_text FROM episode_topics WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _check_and_refill_queue(
    db_path: Path,
    threshold: int,
    auto_approve: bool,
    assets_root: Path | None = None,
) -> dict:
    """
    Inspect topic queue depth across all active channels.

    For each active channel: first check its declared deliverable/bundle
    ready-ahead buffer(s) (hypogeum.buffer.compute_buffer_state) — a
    channel whose EVERY declared bundle already reports "hold" or "excess"
    (buffer target met or exceeded) is skipped entirely for this iteration,
    regardless of topic-backlog depth. This is a DIFFERENT gate than the
    queue-depth check below: queue depth is about the topic *backlog* (raw
    ideas awaiting production), the buffer gate is about *finished,
    publish-ready deliverables/bundles* already ahead of schedule — see
    hypogeum/buffer.py's own docstring, including its documented Channel
    Package schema gap. hypogeum.buffer is fully channel/bundle-generic —
    it has no notion of "episode", "Shorts", "weekly", or any Hypogeum
    constant; _resolve_bundle_definitions_for_channel() below is this
    file's own narrow, explicit, documented stand-in for a real Channel-
    Package-driven bundle compiler (which does not exist yet — see that
    function's docstring). A channel with no resolvable bundle definition
    is never buffer-gated at all (falls through to the queue-depth logic
    below, unaffected) — never guessed at.

    Only when a channel's buffer gate allows it (or the channel has no
    resolvable bundle definition) does the existing queue-depth-driven
    discovery/auto-approve logic below run, otherwise unchanged: if the
    number of queued topics is below `threshold`, run a discovery cycle to
    generate fresh candidates. If `auto_approve` is True, the top-scored
    candidate is automatically approved and enqueued, closing the loop
    without operator input.

    Duplicate-topic errors from enqueue_topic() are handled gracefully —
    treated as a successful enqueue of an already-queued topic.

    Returns:
        {
          "channels_checked": int,
          "channels_refilled": int,
          "cycles_run": list[str],     # cycle_ids
          "topics_enqueued": list[str] # topic texts (only when auto_approve)
          "buffer_gated": list[str],   # channel_ids skipped this iteration by the buffer gate
          "buffer_states": {channel_id: {bundle_id: {"action": ..., "buffer_count": ...,
                                                       "target_buffer_count": ..., "reason": ...}}}
        }
    """
    from hypogeum.channel_config import list_channel_profiles
    from hypogeum.topic_discovery import run_discovery_cycle, approve_candidate
    from hypogeum.db import get_connection
    from hypogeum.buffer import compute_buffer_state, gather_channel_demand_items, resolve_complete_item_ids_for_channel

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

        # ── Per-channel operator pause (2026-08-23) ─────────────────────
        # ChannelOperationsProfile.operator_enabled already gates real
        # pipeline execution (hypogeum.worker_integration.
        # build_gated_pipeline_executor()'s own operator_enabled check) --
        # closing the same gap here means a channel an operator has
        # disabled never even triggers a fresh (paid) discovery cycle or
        # auto-approves a new topic in the first place, rather than
        # discovering/queuing work that would only be refused later at
        # claim time.
        #
        # 2026-08-23 (autonomy safety semantics correction): a channel
        # with no ChannelOperationsProfile at all (or one this read
        # couldn't parse) now fails CLOSED here -- discovery is a real,
        # paid action (topic_discovery.run_discovery_cycle() makes real
        # LLM/Perplexity calls) and approve_candidate() (this loop's own
        # auto-approve path below) has no ChannelOperationsProfile gate
        # of its own at all, so this is the ONLY gate standing between
        # an unconfigured channel and autonomous spend/topic-approval.
        # Previously fail-open-on-absence, matching the buffer/bundle-
        # definitions gates' own (correct, for THOSE non-mutating checks)
        # behavior -- but "no restriction configured" is the wrong
        # default for an action that spends money and mutates state.
        from hypogeum.channel_config import get_channel_operations_profile
        try:
            ops_profile = get_channel_operations_profile(channel_id, db_path=db_path)
        except RuntimeError as exc:
            log.error("discovery_operator_enabled_check_error channel=%s error=%s", channel_id, exc)
            ops_profile = None
        if ops_profile is None or not ops_profile.operator_enabled:
            log.info(
                "discovery_operator_disabled_or_no_profile channel=%s has_profile=%s — skipping refill",
                channel_id, ops_profile is not None,
            )
            continue

        # ── Deliverable/bundle buffer gate (Step 0 discovery-refill gate
        # for THIS channel only — tick()/consume_scheduled_runs()/poll()
        # below, and every other channel's own iteration of this loop, are
        # never touched by this) ────────────────────────────────────────
        bundle_definitions = _resolve_bundle_definitions_for_channel(channel_id, db_path=db_path)
        if bundle_definitions:
            try:
                bundle_states = []
                for bundle_def in bundle_definitions:
                    item_ids = resolve_complete_item_ids_for_channel(channel_id, db_path=db_path)
                    demand_items = gather_channel_demand_items(
                        bundle_def, item_ids=item_ids,
                        # No generic current-publishing-slot resolver exists
                        # yet (documented schema gap in hypogeum/buffer.py) —
                        # passing empty here is this call site's own
                        # conscious, temporary, documented choice, never a
                        # hidden default inside hypogeum.buffer itself.
                        current_cycle_item_ids=frozenset(),
                        db_path=db_path, assets_root=effective_assets_root,
                    )
                    bundle_states.append(compute_buffer_state(bundle_def, demand_items))

                result["buffer_states"][channel_id] = {
                    b.bundle_id: {
                        "action": b.action, "buffer_count": b.buffer_count,
                        "target_buffer_count": b.target_buffer_count, "reason": b.reason,
                    }
                    for b in bundle_states
                }

                # Gate this channel's discovery refill only if EVERY one of
                # its declared bundles already has a sufficient/excess
                # buffer — a channel with two bundles where only one still
                # needs replenishment must not be starved on the other's
                # account (discovery itself is not yet bundle-scoped; see
                # hypogeum/buffer.py's SCHEMA GAP note).
                if bundle_states and all(b.action != "enqueue" for b in bundle_states):
                    result["buffer_gated"].append(channel_id)
                    log.info(
                        "discovery_buffer_gated channel=%s bundles=%s",
                        channel_id, result["buffer_states"][channel_id],
                    )
                    continue
            except Exception as exc:
                # Fail closed, matching this codebase's pervasive ambiguity
                # policy (production_output_gate.py, the Zernio adapter,
                # media staging): an unknown buffer state must never be
                # treated as "buffer empty, safe to enqueue more work".
                log.error("buffer_gate_error channel=%s error=%s — gating discovery (fail closed)", channel_id, exc)
                result["buffer_gated"].append(channel_id)
                result["buffer_states"][channel_id] = {"error": str(exc)}
                continue

        # ── Queue depth check ─────────────────────────────────────────────
        # A shorts-only channel (production_cadence.videos_per_week == 0,
        # shorts_per_week >= 1) publishes one Short per day, so it needs a
        # deeper topic backlog than the single global default. Use the
        # larger of the global threshold and the channel's weekly Short
        # count so a week's worth of topics is always staged.
        effective_threshold = threshold
        try:
            _cad = (ops_profile.production_cadence or {}) if ops_profile is not None else {}
            _spw = _cad.get("shorts_per_week")
            if _cad.get("videos_per_week") == 0 and isinstance(_spw, int) and _spw >= 1:
                effective_threshold = max(threshold, _spw)
        except Exception:
            pass

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

        if depth >= effective_threshold:
            log.debug(
                "discovery_queue_sufficient channel=%s depth=%d threshold=%d",
                channel_id, depth, effective_threshold,
            )
            continue

        # ── Queue is below threshold — run discovery cycle ────────────────
        log.info(
            "discovery_triggered channel=%s depth=%d threshold=%d",
            channel_id, depth, effective_threshold,
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

        # ── v2 Discovery Director already selected + routed the winner ────
        # (it honours AUTO_DISCOVERY_AUTO_APPROVE itself). Report what it did
        # and do NOT run the v1 approve path — that would double-enqueue.
        if cycle.get("already_routed"):
            if cycle.get("already_enqueued"):
                topic_text = _topic_text_for_id(cycle.get("enqueued_topic_id"), db_path)
                result["topics_enqueued"].append(topic_text or cycle.get("enqueued_topic_id"))
                log.info(
                    "discovery_auto_enqueued channel=%s topic=%r topic_id=%s",
                    channel_id, topic_text, cycle.get("enqueued_topic_id"),
                )
            elif cycle.get("selected_candidate_id"):
                log.info(
                    "discovery_selected_pending_approval channel=%s cycle_id=%s "
                    "opportunity=%s route=%s",
                    channel_id, cycle_id, cycle.get("selected_candidate_id"),
                    cycle.get("route_status", ""),
                )
            else:
                log.info(
                    "discovery_no_viable_candidate channel=%s cycle_id=%s "
                    "(director found %d candidates, none cleared selection)",
                    channel_id, cycle_id, cycle.get("candidate_count", 0),
                )
            continue

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
    assets_root: Path | None = None,
) -> dict:
    """
    Execute one scheduler → consume → poll cycle.

    assets_root: Optional. None (default) resolves to this module's own
        canonical _DEFAULT_DB.parent (_PROJECT_ROOT / ".hypogeum-assets")
        — the SAME constant every other Factory Core module
        (hypogeum.agent, hypogeum.episode_artifacts, hypogeum.pipeline,
        hypogeum.openrouter, hypogeum.perplexity, hypogeum.source_enhancement
        all independently define their own identical _ASSETS_ROOT this
        same way) already treats as canonical — NEVER derived from the
        caller's own db_path, which may legitimately point somewhere else
        entirely (a test's isolated tmp_path database, a bounded run's
        alternate location) without the episode asset tree living
        alongside it. An explicit assets_root always wins when given.

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
    #
    # 2026-08-23 scoped pause exemption: an explicitly authorized, still-
    # active bounded_run_id may claim through queue_paused WITHOUT ever
    # touching the global flag itself -- the global pause otherwise stays
    # in full effect for every unbounded caller, exactly as before. This
    # never widens what a bounded run can do: claim_next_bounded() (Step
    # 3's own claim path when bounded_run_id is set) already enforces its
    # own max_episode_claims allowance and publishing_allowed gate
    # independently of this check -- the exemption only decides WHETHER
    # this one, already-capped run is allowed to attempt a claim at all
    # while the queue is paused for everyone else. Re-validated here
    # (not merely trusted from run_worker()'s own startup check) so a
    # caller invoking run_iteration() directly, out of band, can never
    # exercise this exemption with an unknown/inactive/exhausted
    # bounded_run_id.
    bypass_pause_for_bounded_run = (
        queue_paused and bounded_run_id is not None
        and _validate_bounded_run_ready(bounded_run_id, db_path) is None
    )
    if bypass_pause_for_bounded_run:
        log.warning(
            "poll queue_paused_but_bounded_run_exempted bounded_run_id=%s",
            bounded_run_id,
        )
    if (queue_paused and not bypass_pause_for_bounded_run) or not clearance.cleared:
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
        # project_root is ONE LEVEL UP from assets_root: worker_integration's
        # _episode_dir() joins project_root / ".hypogeum-assets" / "episodes"
        # / episode_id, while this module's own assets_root convention
        # already POINTS AT ".hypogeum-assets" itself (see run_iteration()'s
        # own docstring). Never threading this through left every
        # WorkerAttempt write falling back to worker_integration's own
        # _PROJECT_ROOT default (the real repository) regardless of what
        # assets_root a caller supplied -- silently ignoring test isolation
        # and writing real files under the real .hypogeum-assets/episodes/
        # tree for whatever episode_id the caller was using, even a
        # throwaway/synthetic one.
        _project_root = Path(assets_root).parent if assets_root is not None else None
        gated_executor = build_gated_pipeline_executor(
            db_path=db_path, bounded_run_id=bounded_run_id, project_root=_project_root,
        )
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

    # Step 3.5: autonomous Zernio publishing — submit any channel's
    # newly-completed, not-yet-published bundle. Runs regardless of
    # queue_paused (publishing already-completed content is a different
    # concern than pausing NEW paid production — see
    # _process_channel_publishing()'s own docstring) but is HARD-GATED by
    # `not dry_run` FIRST, before even checking the master switch — a
    # worker dry run must never stage, upload, submit, publish, schedule,
    # or create any real provider write, and this gate does not rely on
    # the global factory-clearance check alone (clearance answers a
    # different question — "is the factory allowed to run at all" — not
    # "is this specific iteration a dry run"). Only when NOT a dry run AND
    # the master switch is enabled AND the factory is cleared (the SAME
    # clearance check Step 3 already computed, reused not duplicated) does
    # this step ever call into the trigger. Absent configuration/bindings
    # fail closed per-channel inside the function itself — never raised
    # here, never crashes this loop.
    publishing_result: dict = {}
    if dry_run:
        publishing_result = {"skipped_reason": "dry_run"}
        log.debug("publishing_skipped_dry_run")
    elif getattr(config, "AUTO_ZERNIO_PUBLISHING_ENABLED", False):
        if clearance.cleared:
            try:
                effective_assets_root = Path(assets_root) if assets_root is not None else _DEFAULT_DB.parent
                publishing_result = _process_channel_publishing(db_path=db_path, assets_root=effective_assets_root)
                if publishing_result.get("channels_with_activity"):
                    log.info(
                        "publishing_summary channels_with_activity=%d results=%s",
                        publishing_result["channels_with_activity"],
                        publishing_result["results_by_channel"],
                    )
            except Exception as exc:
                log.error("publishing_error error=%s", exc, exc_info=True)
        else:
            publishing_result = {"skipped_reason": "not_cleared"}
            log.info("publishing_skipped_not_cleared reason=%s", clearance.reason)
    else:
        publishing_result = {"skipped_reason": "auto_zernio_publishing_disabled"}

    # Runtime health sweep — run_all_checks() emits an OIS signal (and, if a
    # notification channel is configured, an operator alert via
    # hypogeum.notifications.route_signal) for worker/queue/stuck-run/
    # repeated-failure/provider/disk/memory/backup problems. Previously only
    # run when the dashboard health endpoint was hit — never periodically.
    if not dry_run and iteration % _HEALTH_CHECK_EVERY_N == 0:
        try:
            from hypogeum.observability import run_all_checks
            _hc = run_all_checks(db_path=db_path, config=config)
            if not _hc.get("healthy", True):
                _bad = [c["check"] for c in _hc.get("checks", []) if not c.get("healthy")]
                log.warning("health_sweep unhealthy=%s", _bad)
            else:
                log.debug("health_sweep ok")
        except Exception as exc:  # noqa: BLE001
            log.error("health_sweep_error error=%s", exc)

    # Step 4: background analytics sync — every _ANALYTICS_SYNC_EVERY_N iterations
    analytics_synced = False
    scheduling_evidence_result: dict = {}
    if not dry_run and iteration % _ANALYTICS_SYNC_EVERY_N == 0:
        log.info("analytics_sync_triggered iteration=%d", iteration)
        _sync_analytics_background(db_path=db_path, config=config)
        # Refresh the Zernio account snapshot (connection health + follower
        # counts) that the dashboard's platform pages read non-blockingly.
        try:
            from hypogeum.publishing.zernio_accounts import sync_zernio_accounts
            _zr = sync_zernio_accounts(db_path)
            if _zr["ok"]:
                log.info("zernio_accounts_synced count=%d analytics_access=%s",
                         _zr["synced"], _zr["has_analytics_access"])
            else:
                log.debug("zernio_accounts_sync_skip reason=%s", _zr["error"])
        except Exception as exc:  # noqa: BLE001
            log.error("zernio_accounts_sync_error error=%s", exc)
        analytics_synced = True
        # Close the self-learning loop right after fresh analytics land.
        try:
            _run_learning_cycle(db_path=db_path)
        except Exception as exc:
            log.error("learning_cycle_sweep_error error=%s", exc, exc_info=True)
        try:
            scheduling_evidence_result = _refresh_scheduling_evidence_for_all_channels(db_path=db_path)
        except Exception as exc:
            log.error("scheduling_evidence_refresh_sweep_error error=%s", exc, exc_info=True)

    # Archive relocation sweep — move any episode that is now published but
    # whose assets are still in the working area (the scheduled-release case:
    # the pipeline's own relocation step no-ops while the publish is pending).
    # Non-blocking; never affects the iteration outcome.
    relocation_sweep = []
    try:
        from hypogeum.archive_relocation import relocate_pending_published_episodes
        relocation_sweep = relocate_pending_published_episodes(db_path=db_path, limit=2)
        if relocation_sweep:
            log.info("archive_relocation_sweep done=%d", len(relocation_sweep))
    except Exception as exc:  # noqa: BLE001
        log.error("archive_relocation_sweep_error error=%s", exc)

    # Signal-noise sweep — auto-resolve informational signals and any signal
    # tied to an already-published episode, so the operator's attention panel
    # only ever shows conditions that need a human. Non-blocking.
    try:
        from hypogeum.signal_digest import auto_resolve_stale_signals
        _sig_cleared = auto_resolve_stale_signals(db_path=db_path)
        if _sig_cleared:
            log.info("signal_noise_sweep resolved=%d", _sig_cleared)
    except Exception as exc:  # noqa: BLE001
        log.error("signal_noise_sweep_error error=%s", exc)

    poll_status = proc.status if proc else (
        "paused" if (queue_paused and not bypass_pause_for_bounded_run)
        else ("not_cleared" if not clearance.cleared else "empty")
    )
    return {
        "scheduled_runs_created": len(sr_ids),
        "queue_items_created":    len(queue_ids),
        "poll_status":            poll_status,
        "relocation_sweep":       len(relocation_sweep),
        "poll_stop_reason":       poll_result.stop_reason,
        "dry_run":                dry_run,
        "discovery":              discovery_result,
        "publishing":             publishing_result,
        "analytics_synced":       analytics_synced,
        "scheduling_evidence":    scheduling_evidence_result,
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
    assets_root: Path | None = None,
) -> int:
    """
    Run the worker loop. Returns exit code.

    assets_root: Optional. None (default) preserves the canonical
        _DEFAULT_DB.parent resolution — see run_iteration()'s own
        docstring. Forwarded unchanged into every run_iteration() call.

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

    # Singleton enforcement (2026-08-23, corrected same day — see
    # _acquire_worker_singleton_lock()'s own docstring): every mutation-
    # capable invocation of this process acquires the SAME shared lock,
    # daemon or one-shot alike. No `once`/gated exemption.
    conflict_reason = _acquire_worker_singleton_lock()
    if conflict_reason is not None:
        log.error("worker_singleton_conflict %s", conflict_reason)
        return 1

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

        # Heartbeat (2026-08-23): a periodic liveness timestamp distinct
        # from worker_status -- a worker hung or crashed mid-iteration
        # (no atexit, no signal) leaves worker_status stuck at its last
        # written value forever, indistinguishable from "still working
        # normally on something slow". Writing wall-clock "now" every
        # iteration, unconditionally, before any step that could hang or
        # raise, lets any external monitor (dashboard, launchd health
        # check) detect a stalled worker via is_worker_heartbeat_stale()
        # below, without needing to reason about worker_status's own
        # value at all.
        _wc_set("worker_heartbeat_at", _now_utc().isoformat(), db_path)
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
                assets_root=assets_root,
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
    # Explicit cleanup on the normal graceful-exit path (2026-08-23) --
    # the atexit handler _acquire_worker_singleton_lock() registered is a
    # safety net for a crash or an unhandled signal that skips past this
    # point, but it only fires at real PROCESS exit. A caller that invokes
    # run_worker() directly and keeps running afterward (e.g. many
    # sequential calls within one long-lived test process) would
    # otherwise leave a stale-but-still-matching PID file on disk
    # indefinitely, since atexit never fires between such calls -- exactly
    # what the singleton check depends on being accurate.
    _release_worker_singleton_lock()
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


def _validate_bounded_run_ready(
    bounded_run_id: str, db_path: Path, episode_id: str | None = None,
) -> str | None:
    """
    Read-only pre-flight check for an already-provisioned bounded run.

    Returns None when the bounded run is ready to claim; otherwise a
    human-readable reason. Used by every bounded CLI entry point
    (--gated-episode-id + --bounded-run-id, and the plain autonomous daemon
    loop) to fail closed *before* starting any claiming, rather than a
    daemon loop silently reporting "empty" every iteration with no
    diagnostic. The real, authoritative enforcement remains inside
    hypogeum.queue.claim_next_bounded() regardless of this check — this
    function never provisions, claims, or mutates anything.

    episode_id (2026-08-23, same-episode bounded recovery): optional.
    None (default — every generic daemon-loop call site, which cannot
    know in advance which episode_id claim_next_queue_item() will
    actually pick) preserves the original, conservative contract exactly
    — status must be 'active'. When the caller already knows the specific
    target episode_id (--gated-episode-id's own call site, which always
    does), an 'exhausted' bounded run is ALSO accepted here, but only if
    hypogeum.db.bounded_run_already_claimed_episode() confirms this
    bounded run already owns that exact episode_id — i.e. only for a
    genuine continuation, never a general "exhausted is fine" relaxation.
    This mirrors claim_next_bounded()'s own real enforcement so a caller
    that already knows its target gets an accurate, fast pre-flight
    answer instead of always deferring to the claim transaction itself.
    """
    from hypogeum.db import bounded_run_already_claimed_episode, get_bounded_run, get_connection

    conn = get_connection(db_path)
    try:
        bounded_run = get_bounded_run(conn, bounded_run_id)
        if bounded_run is None:
            return (
                f"unknown bounded_run_id: {bounded_run_id!r} — provision it first "
                f"with --provision-bounded-run"
            )
        if bounded_run["status"] == "active":
            return None
        if (
            bounded_run["status"] == "exhausted"
            and episode_id is not None
            and bounded_run_already_claimed_episode(conn, bounded_run_id, episode_id)
        ):
            return None
    finally:
        conn.close()
    return (
        f"bounded_run_id {bounded_run_id!r} is not active (status={bounded_run['status']!r})"
        + (
            f" and has not already claimed episode_id={episode_id!r} — a continuation of "
            f"an already-claimed episode would still be permitted"
            if episode_id is not None else ""
        )
    )


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
        _validate_bounded_run_ready() for a fast, clear CLI error, now
        passing episode_id so a same-episode continuation of an already-
        'exhausted' bounded run is correctly recognized as ready; the real
        enforcement is inside hypogeum.queue.claim_next_bounded()
        regardless) — this function never provisions one itself.

    Existing-queued-row reuse (2026-08-23, same-episode bounded recovery):
        before enqueueing anything, checks for an ALREADY-queued run_queue
        row for this exact (channel_id, episode_id) — e.g. the row an
        earlier failure's auto-retry mechanism created for the SAME
        episode_id. When one exists, it is reused as-is (no new row, no
        new topic claim) and this call proceeds straight to poll(); a
        brand-new row is enqueued only when no queued row for this
        episode already exists, exactly matching prior behavior for a
        genuinely fresh manual invocation.
    """
    from hypogeum.config import Config
    from hypogeum.db import get_connection, initialize_schema
    from hypogeum.queue import enqueue, poll
    from hypogeum.worker_integration import build_gated_pipeline_executor

    try:
        initialize_schema(db_path)
        config = Config()
    except Exception as exc:
        print(f"startup error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if bounded_run_id is not None:
        reason = _validate_bounded_run_ready(bounded_run_id, db_path, episode_id=episode_id)
        if reason is not None:
            print(reason, file=sys.stderr)
            return 1

    # Singleton enforcement (2026-08-23): a gated/one-shot invocation is
    # exactly as mutation-capable as the daemon loop (claims from
    # run_queue, runs the real pipeline, may stage/publish) and shares
    # the SAME lock -- see _acquire_worker_singleton_lock()'s own
    # docstring for why the prior "one-shot invocations are exempt"
    # design was wrong. Acquired here, after the cheap validity checks
    # above but before any run_queue mutation (existing-row lookup below
    # is read-only; enqueue()/poll() are not).
    conflict_reason = _acquire_worker_singleton_lock()
    if conflict_reason is not None:
        print(f"worker_singleton_conflict: {conflict_reason}", file=sys.stderr)
        return 1

    try:
        conn = get_connection(db_path)
        try:
            existing_row = conn.execute(
                "SELECT queue_id FROM run_queue WHERE channel_id = ? AND episode_id = ? "
                "AND status = 'queued' ORDER BY queued_at DESC LIMIT 1",
                (channel_id, episode_id),
            ).fetchone()
        finally:
            conn.close()

        if existing_row is not None:
            queue_id = existing_row["queue_id"]
            log.info(
                "gated_cli_reusing_existing_queue_row channel=%s episode_id=%s queue_id=%s",
                channel_id, episode_id, queue_id,
            )
        else:
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
    finally:
        _release_worker_singleton_lock()


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
