"""
hypogeum/observability.py — Runtime Observability (Phase 3 Production
Hardening, P0.3)

Signals layer + alerting layer, treated as one capability per the
Production Hardening architecture review — monitoring without alerting
is a dashboard nobody watches; alerting without monitoring has nothing to
threshold against.

Alerting reuses the existing OIS signal substrate
(hypogeum/ois.py::emit_signal()) rather than a parallel notification
path: emit_signal() already handles persistence, per-severity cooldown
dedup, and routing to Telegram/email via hypogeum/notifications.py. This
module's job is purely detection — inspecting worker/queue/run/disk/
memory/backup state and calling emit_signal() when a threshold is
crossed. None of these signals are blocking=True — Runtime Observability
alerts operators, it does not halt production runs (that would be a
different, much bigger decision than a monitoring capability should make
on its own).

Scheduler heartbeat is not checked separately from worker heartbeat:
run_worker.py's scheduler and queue loop are the same process (CLAUDE.md:
"scheduler -> queue -> pipeline loop"), so one heartbeat covers both.

Stuck-run detection here is detection only — the fix (reconciliation) is
Phase 3 P0.4, Recovery & Self-Healing.

Public API:
    check_worker_heartbeat(db_path=None, config=None)  -> CheckResult
    check_queue_growth(db_path=None, config=None)       -> CheckResult
    check_stuck_runs(db_path=None, config=None)         -> CheckResult
    check_repeated_failures(db_path=None, config=None)  -> CheckResult
    check_provider_failures(db_path=None, config=None)  -> CheckResult
    check_disk_usage(path=None, config=None)            -> CheckResult
    check_memory_usage(config=None)                     -> CheckResult
    check_backup_health(backup_dir=None, config=None)   -> CheckResult
    run_all_checks(db_path=None, config=None)           -> dict
        # Runs every check above, emits an OIS signal for any that fail,
        # returns an aggregate report. This is the data source behind the
        # dashboard's health endpoint.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.db import get_connection
from hypogeum.ois import emit_signal

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_WORKER_PID_FILE = _PROJECT_ROOT / ".hypogeum-assets" / "worker.pid"

_SOURCE_SYSTEM = "runtime_observability"

# Non-terminal run/queue states — used by stuck-run detection.
_NON_TERMINAL_RUN_STATUSES = ("running",)
_NON_TERMINAL_QUEUE_STATUSES = ("claimed",)
_NON_TERMINAL_STAGE_STATUSES = ("running",)

# Provider names to scan for in stage_executions.error_message when
# detecting elevated provider failure rates. Matches hypogeum/config.py's
# known provider API keys.
_KNOWN_PROVIDERS = (
    "openrouter", "muapi", "elevenlabs", "perplexity",
    "higgsfield", "byteplus", "runway", "kling", "openai",
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _resolve_config(config) -> "Config":
    if config is not None:
        return config
    try:
        return Config()
    except Exception:
        return None  # emit_signal() tolerates config=None (skips notification)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    check: str
    healthy: bool
    detail: str
    signal_id: Optional[str] = None


def _emit_if_unhealthy(
    result: CheckResult,
    signal_type: str,
    severity: str,
    config,
    db_path: Optional[Path],
) -> CheckResult:
    if result.healthy:
        return result
    signal_id = emit_signal(
        signal_type=signal_type,
        severity=severity,
        message=result.detail,
        channel_id=None,
        source_system=_SOURCE_SYSTEM,
        blocking=False,
        db_path=db_path,
        config=config,
    )
    result.signal_id = signal_id
    return result


# ---------------------------------------------------------------------------
# Worker / scheduler heartbeat
# ---------------------------------------------------------------------------

def check_worker_heartbeat(
    db_path: Optional[Path] = None,
    config=None,
) -> CheckResult:
    """
    worker_control's worker_status row is touched every iteration of
    run_worker.py's main loop, whether or not work was found — its
    updated_at is therefore a real heartbeat, not just a state-transition
    marker. Stale beyond WORKER_HEARTBEAT_STALE_SECONDS means the worker
    (and scheduler — same process) is not running or has hung.
    """
    path = _resolve_db_path(db_path)
    cfg = _resolve_config(config)
    threshold = getattr(cfg, "WORKER_HEARTBEAT_STALE_SECONDS", 300) if cfg else 300

    try:
        conn = get_connection(path)
        try:
            row = conn.execute(
                "SELECT value, updated_at FROM worker_control WHERE key = 'worker_status'"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult("worker_heartbeat", False, f"could not read worker_control: {exc}")

    if row is None:
        return CheckResult(
            "worker_heartbeat", False,
            "no worker_control row found — worker has never started",
        )

    updated_at = _parse_iso(row["updated_at"])
    if updated_at is None:
        return CheckResult(
            "worker_heartbeat", False,
            f"worker_control.updated_at unparseable: {row['updated_at']!r}",
        )

    age_seconds = (datetime.now(tz=timezone.utc) - updated_at).total_seconds()
    if age_seconds > threshold:
        return CheckResult(
            "worker_heartbeat", False,
            f"worker heartbeat stale: last update {age_seconds:.0f}s ago "
            f"(threshold {threshold}s), worker_status={row['value']!r}",
        )
    return CheckResult(
        "worker_heartbeat", True,
        f"worker heartbeat fresh: {age_seconds:.0f}s ago, worker_status={row['value']!r}",
    )


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Queue growth
# ---------------------------------------------------------------------------

def check_queue_growth(
    db_path: Optional[Path] = None,
    config=None,
) -> CheckResult:
    """Is run_queue backing up faster than it drains."""
    path = _resolve_db_path(db_path)
    cfg = _resolve_config(config)
    threshold = getattr(cfg, "QUEUE_GROWTH_WARNING_THRESHOLD", 20) if cfg else 20

    try:
        conn = get_connection(path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM run_queue WHERE status = 'queued'"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult("queue_growth", False, f"could not read run_queue: {exc}")

    queued_count = row["cnt"] if row else 0
    if queued_count > threshold:
        return CheckResult(
            "queue_growth", False,
            f"{queued_count} items queued, exceeds threshold {threshold} — "
            "queue may be backing up faster than it drains",
        )
    return CheckResult("queue_growth", True, f"{queued_count} items queued")


# ---------------------------------------------------------------------------
# Stuck runs (detection only — Phase 3 P0.4 does the reconciliation)
# ---------------------------------------------------------------------------

def check_stuck_runs(
    db_path: Optional[Path] = None,
    config=None,
) -> CheckResult:
    """
    A runs/run_queue/stage_executions row stuck in a non-terminal status
    past STUCK_RUN_THRESHOLD_MINUTES is stale. Detection only — this
    check does not reconcile anything, see hypogeum.self_healing (P0.4).
    """
    path = _resolve_db_path(db_path)
    cfg = _resolve_config(config)
    threshold_minutes = getattr(cfg, "STUCK_RUN_THRESHOLD_MINUTES", 120) if cfg else 120
    cutoff = datetime.now(tz=timezone.utc).timestamp() - (threshold_minutes * 60)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

    try:
        conn = get_connection(path)
        try:
            stuck_runs = conn.execute(
                "SELECT run_id FROM runs WHERE status IN "
                f"({_qmarks(_NON_TERMINAL_RUN_STATUSES)}) AND started_at < ?",
                (*_NON_TERMINAL_RUN_STATUSES, cutoff_iso),
            ).fetchall()
            # A stage is only "stuck" if its run is still live. A stage row
            # left 'running' because its run failed/was abandoned hours ago
            # is stale bookkeeping, not a stuck stage — otherwise the health
            # sweep re-flags it every cycle forever.
            stuck_stages = conn.execute(
                "SELECT se.stage_exec_id FROM stage_executions se "
                f"WHERE se.status IN ({_qmarks(_NON_TERMINAL_STAGE_STATUSES)}) "
                "AND se.started_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.run_id = se.run_id "
                "  AND LOWER(r.status) IN ('failed','error','cancelled','complete',"
                "  'completed','published','superseded'))",
                (*_NON_TERMINAL_STAGE_STATUSES, cutoff_iso),
            ).fetchall()
            stuck_queue = conn.execute(
                "SELECT queue_id FROM run_queue WHERE status IN "
                f"({_qmarks(_NON_TERMINAL_QUEUE_STATUSES)}) AND claimed_at < ?",
                (*_NON_TERMINAL_QUEUE_STATUSES, cutoff_iso),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult("stuck_runs", False, f"could not query stuck-run tables: {exc}")

    total = len(stuck_runs) + len(stuck_stages) + len(stuck_queue)
    if total > 0:
        return CheckResult(
            "stuck_runs", False,
            f"{len(stuck_runs)} stuck run(s), {len(stuck_stages)} stuck "
            f"stage(s), {len(stuck_queue)} stuck queue item(s) — all "
            f"non-terminal past {threshold_minutes} minutes",
        )
    return CheckResult("stuck_runs", True, "no stuck runs, stages, or queue items")


def _qmarks(values: tuple) -> str:
    return ",".join("?" for _ in values)


# ---------------------------------------------------------------------------
# Repeated failures
# ---------------------------------------------------------------------------

def check_repeated_failures(
    db_path: Optional[Path] = None,
    config=None,
) -> CheckResult:
    """
    Same stage failing across REPEATED_FAILURE_THRESHOLD or more of the
    most recent runs that touched it — distinct from a single transient
    failure already handled by bounded auto-retry.
    """
    path = _resolve_db_path(db_path)
    cfg = _resolve_config(config)
    threshold = getattr(cfg, "REPEATED_FAILURE_THRESHOLD", 3) if cfg else 3

    try:
        conn = get_connection(path)
        try:
            stage_names = [
                r["stage_name"] for r in
                conn.execute("SELECT DISTINCT stage_name FROM stage_executions").fetchall()
            ]
            offenders = []
            for stage_name in stage_names:
                recent = conn.execute(
                    "SELECT status FROM stage_executions WHERE stage_name = ? "
                    "ORDER BY started_at DESC LIMIT ?",
                    (stage_name, threshold),
                ).fetchall()
                if len(recent) >= threshold and all(r["status"] == "failed" for r in recent):
                    offenders.append(stage_name)
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult("repeated_failures", False, f"could not query stage_executions: {exc}")

    if offenders:
        return CheckResult(
            "repeated_failures", False,
            f"stage(s) failed {threshold}+ consecutive times: {', '.join(offenders)}",
        )
    return CheckResult("repeated_failures", True, "no stage has repeated consecutive failures")


# ---------------------------------------------------------------------------
# Provider failures
# ---------------------------------------------------------------------------

def check_provider_failures(
    db_path: Optional[Path] = None,
    config=None,
) -> CheckResult:
    """
    Elevated failure rate for a specific provider, inferred from recent
    stage_executions.error_message text — distinct from a single job's
    own retry.
    """
    path = _resolve_db_path(db_path)
    cfg = _resolve_config(config)
    threshold = getattr(cfg, "PROVIDER_FAILURE_THRESHOLD", 3) if cfg else 3

    try:
        conn = get_connection(path)
        try:
            rows = conn.execute(
                "SELECT error_message FROM stage_executions "
                "WHERE status = 'failed' AND error_message IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 50"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult("provider_failures", False, f"could not query stage_executions: {exc}")

    counts: dict[str, int] = {}
    for row in rows:
        text = (row["error_message"] or "").lower()
        for provider in _KNOWN_PROVIDERS:
            if provider in text:
                counts[provider] = counts.get(provider, 0) + 1

    offenders = [p for p, c in counts.items() if c >= threshold]
    if offenders:
        return CheckResult(
            "provider_failures", False,
            f"provider(s) with {threshold}+ recent failures: "
            + ", ".join(f"{p}={counts[p]}" for p in offenders),
        )
    return CheckResult("provider_failures", True, "no provider has an elevated failure rate")


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------

def check_disk_usage(
    path: Optional[Path] = None,
    config=None,
) -> CheckResult:
    """Free disk space on the filesystem backing .hypogeum-assets/."""
    target = path or (_PROJECT_ROOT / ".hypogeum-assets")
    cfg = _resolve_config(config)
    warning_pct = getattr(cfg, "DISK_FREE_WARNING_PCT", 10.0) if cfg else 10.0

    try:
        target.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return CheckResult("disk_usage", False, f"could not stat {target}: {exc}")

    free_pct = (usage.free / usage.total) * 100 if usage.total else 0.0
    if free_pct < warning_pct:
        return CheckResult(
            "disk_usage", False,
            f"{free_pct:.1f}% free, below {warning_pct}% threshold "
            f"({usage.free / (1024**3):.1f}GB free of {usage.total / (1024**3):.1f}GB)",
        )
    return CheckResult("disk_usage", True, f"{free_pct:.1f}% free")


# ---------------------------------------------------------------------------
# Memory usage (worker process RSS — no psutil dependency in this project)
# ---------------------------------------------------------------------------

def check_memory_usage(config=None) -> CheckResult:
    """
    RSS of the worker process (located via its PID file), read through
    `ps` rather than psutil (not a project dependency). A missing PID
    file (worker not running) is reported as healthy=True — that
    condition is check_worker_heartbeat's job, not this check's.
    """
    cfg = _resolve_config(config)
    warning_mb = getattr(cfg, "WORKER_MEMORY_WARNING_MB", 2048) if cfg else 2048

    if not _WORKER_PID_FILE.exists():
        return CheckResult("memory_usage", True, "no worker PID file — worker not running")

    try:
        pid = int(_WORKER_PID_FILE.read_text().strip())
    except (ValueError, OSError) as exc:
        return CheckResult("memory_usage", False, f"could not read worker PID file: {exc}")

    try:
        output = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return CheckResult("memory_usage", False, f"could not run ps for pid {pid}: {exc}")

    stdout = output.stdout.strip()
    if output.returncode != 0 or not stdout:
        return CheckResult(
            "memory_usage", True,
            f"worker pid {pid} not found in process table (already exited)",
        )

    rss_kb = int(stdout)
    rss_mb = rss_kb / 1024
    if rss_mb > warning_mb:
        return CheckResult(
            "memory_usage", False,
            f"worker RSS {rss_mb:.0f}MB exceeds threshold {warning_mb}MB "
            f"(pid {pid}) — possible memory leak in a long-running process",
        )
    return CheckResult("memory_usage", True, f"worker RSS {rss_mb:.0f}MB (pid {pid})")


# ---------------------------------------------------------------------------
# Backup health
# ---------------------------------------------------------------------------

def check_backup_health(backup_dir: Optional[Path] = None, config=None) -> CheckResult:
    """Surfaces hypogeum.recovery's last scheduled-backup health report."""
    from hypogeum.recovery import read_backup_health

    if backup_dir is None:
        cfg = _resolve_config(config)
        cfg_dir = getattr(cfg, "BACKUP_DIR", None) if cfg else None
        backup_dir = Path(cfg_dir) if cfg_dir else None

    health = read_backup_health(backup_dir=backup_dir)
    if not health.get("healthy"):
        return CheckResult(
            "backup_health", False,
            f"last backup unhealthy or never run: {health.get('error') or health}",
        )
    return CheckResult(
        "backup_health", True, f"last backup healthy, run at {health.get('last_run_at')}",
    )


# ---------------------------------------------------------------------------
# Aggregate — the health-check endpoint's data source
# ---------------------------------------------------------------------------

def run_all_checks(db_path: Optional[Path] = None, config=None) -> dict:
    """
    Run every check, emit an OIS signal (WARNING severity, non-blocking)
    for any that fail, and return an aggregate report.
    """
    cfg = _resolve_config(config)
    path = _resolve_db_path(db_path)

    checks = [
        (check_worker_heartbeat(path, cfg), "worker_offline"),
        (check_queue_growth(path, cfg), "queue_growth"),
        (check_stuck_runs(path, cfg), "stuck_run"),
        (check_repeated_failures(path, cfg), "repeated_failure"),
        (check_provider_failures(path, cfg), "provider_outage"),
        (check_disk_usage(config=cfg), "disk_low"),
        (check_memory_usage(cfg), "memory_high"),
        (check_backup_health(config=cfg), "backup_unhealthy"),
    ]

    results = []
    for result, signal_type in checks:
        results.append(_emit_if_unhealthy(result, signal_type, "WARNING", cfg, path))

    return {
        "checked_at": _now_iso(),
        "healthy": all(r.healthy for r in results),
        "checks": [asdict(r) for r in results],
    }
