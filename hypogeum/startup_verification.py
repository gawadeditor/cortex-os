"""
hypogeum/startup_verification.py — Startup & Shutdown Verification
(Phase 3 Production Hardening, P0.5)

Startup half: wires the preflight checks that already existed as manual
CLI tools (hypogeum/setup_preflight.py, used by run_acceptance_audit.py
and run_production_readiness_audit.py) into automatic execution at
worker startup, rather than requiring an operator to run them by hand.

Design decision — never refuse to start the worker process entirely on
a preflight blocker: the scheduler/queue loop itself has value even if,
say, ELEVENLABS_API_KEY is temporarily missing (discovery and scheduling
don't need it, and individual runs are separately gated by
hypogeum.factory_director.check_run_clearance() before being claimed).
Instead, this emits a loud OIS signal via the same substrate P0.3/P0.4
use, so an operator is notified immediately at startup rather than
discovering the gap via a string of failed runs. deduplicated so a
still-unresolved blocker doesn't re-alert every worker restart.

Shutdown half: verified, not rebuilt. run_worker.py's SIGINT/SIGTERM
handling (a cooperative _shutdown_requested flag, checked at the top of
every loop iteration, after each iteration completes, and during the
inter-iteration sleep) was already graceful before this phase — a signal
never interrupts an in-flight queue claim or pipeline stage; it lets the
current iteration finish (which already completes/fails/releases its
queue claim atomically, per hypogeum/queue.py) before the loop exits.
Interrupting mid-pipeline would be strictly worse — it would leave
partial artifacts in an inconsistent state, which is exactly the kind of
orphaned row P0.4 exists to clean up, not something to invite on
purpose. See tests/test_startup_verification.py for the verification
that documents this rather than reimplementing it.

Public API:
    verify_startup(db_path=None, config=None) -> dict
        # Runs hypogeum.setup_preflight.get_setup_preflight_status(),
        # emits an OIS signal if any blocker is present, and returns the
        # preflight report annotated with whether a signal was emitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.ois import emit_signal
from hypogeum.setup_preflight import get_setup_preflight_status

_SOURCE_SYSTEM = "startup_verification"


def _resolve_config(config):
    if config is not None:
        return config
    try:
        return Config()
    except Exception:
        return None


def verify_startup(db_path: Optional[Path] = None, config=None) -> dict:
    """
    Run preflight checks and alert (never block) on any blocker found.

    Returns the same dict as get_setup_preflight_status(), with one added
    key: "signal_id" (the OIS signal_id if a blocker triggered an alert,
    else None).
    """
    cfg = _resolve_config(config)
    report = get_setup_preflight_status(db_path=db_path)

    signal_id = None
    if not report["ready"]:
        blocker_names = [b["name"] for b in report["blockers"]]
        message = (
            f"Worker starting with {report['blocker_count']} unresolved "
            f"preflight blocker(s): {', '.join(blocker_names)}. The "
            f"scheduler/queue loop will still run, but claimed episodes "
            f"depending on these will fail until resolved."
        )
        signal_id = emit_signal(
            signal_type="startup_blocked",
            severity="CRITICAL",
            message=message,
            channel_id=None,
            source_system=_SOURCE_SYSTEM,
            blocking=False,
            db_path=db_path,
            config=cfg,
            dedup_any_open=True,
        )

    report["signal_id"] = signal_id
    return report
