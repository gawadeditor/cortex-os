"""
hypogeum/run_registry.py — Run Registry + Run Lifecycle Governance (RLG)

Registers every pipeline execution as a durable record and manages its
state through a validated state machine. All state changes are recorded
as immutable run_state_transitions rows.

Two storage layers are written in sync for register/complete/fail:
  1. DB row in runs table (hypogeum/db.py) — queryable
  2. JSON file at .hypogeum-assets/runs/{run_id}.json — human-readable
  3. Append-only index at .hypogeum-assets/runs/index.jsonl — backfill-friendly

Public API:
    register_run(episode_id, config, db_path=None, runs_dir=None) -> str
        Call at pipeline entry. Returns run_id.

    complete_run(run_id, episode_id, db_path=None, runs_dir=None) -> None
        Call on successful pipeline completion.

    fail_run(run_id, episode_id, error_message, db_path=None, runs_dir=None) -> None
        Call in the pipeline except handler.

    transition_run_state(run_id, to_state, triggered_by, trigger_source,
                         reason=None, metadata=None, db_path=None,
                         episode_id=None, channel_id=None) -> str
        Core state-machine function. Validates, records, and applies
        a state change. Raises InvalidRunTransitionError on invalid.

    get_run_state(run_id, db_path=None) -> Optional[str]
    get_run_transition_history(run_id, db_path=None) -> list[dict]

    pause_run(run_id, operator_user_id, reason, db_path=None) -> str
    resume_run(run_id, operator_user_id, db_path=None) -> str

State machine:
    Valid states: queued | running | paused | awaiting_review |
                  awaiting_publish_approval | publishing | completed |
                  failed | blocked

    Terminal states (no outgoing transitions): completed, failed, blocked

    Valid transitions:
        None                        → queued, running
        queued                      → running
        running                     → paused, awaiting_review,
                                      awaiting_publish_approval, publishing,
                                      completed, failed, blocked
        paused                      → running, failed, blocked
        awaiting_review             → awaiting_publish_approval, failed, blocked
        awaiting_publish_approval   → publishing, failed, blocked
        publishing                  → completed, failed, blocked
        completed                   → (terminal)
        failed                      → (terminal)
        blocked                     → (terminal)

P3-04 note:
    awaiting_review and awaiting_publish_approval states are defined here
    as valid transition targets. Their enforcement (requiring human approval
    before unblocking) is implemented in P3-04 (Approval System).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_run,
    insert_run_state_transition,
    get_run_state_transitions,
    update_run_complete,
    update_run_failed,
)
from hypogeum.identifiers import METADATA_VERSION, generate_run_id


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_DEFAULT_RUNS_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "runs"


# ---------------------------------------------------------------------------
# State machine constants
# ---------------------------------------------------------------------------

RUN_STATES = frozenset({
    "queued",
    "running",
    "paused",
    "awaiting_review",
    "awaiting_publish_approval",
    "publishing",
    "completed",
    "failed",
    "blocked",
    "cancelled",   # operator-requested mid-run cancellation
})

# Allowed transitions: from_state (or None for initial) → set of valid to_states
_VALID_TRANSITIONS: dict[Optional[str], frozenset] = {
    None:                        frozenset({"queued", "running"}),
    "queued":                    frozenset({"running"}),
    "running":                   frozenset({
                                     "paused", "awaiting_review",
                                     "awaiting_publish_approval", "publishing",
                                     "completed", "failed", "blocked", "cancelled",
                                 }),
    "paused":                    frozenset({"running", "failed", "blocked", "cancelled"}),
    "awaiting_review":           frozenset({"awaiting_publish_approval", "failed", "blocked", "cancelled"}),
    "awaiting_publish_approval": frozenset({"publishing", "failed", "blocked", "cancelled"}),
    "publishing":                frozenset({"completed", "failed", "blocked", "cancelled"}),
    "completed":                 frozenset(),   # terminal
    "failed":                    frozenset(),   # terminal
    "blocked":                   frozenset(),   # terminal (unblocking deferred to P3-04)
    "cancelled":                 frozenset(),   # terminal — operator-requested cancellation
}

_TERMINAL_STATES = frozenset({"completed", "failed", "blocked", "cancelled"})

# OIS signals emitted on specific state arrivals
_TRANSITION_OIS_SIGNALS: dict[str, tuple[str, str]] = {
    "completed": ("run_completed", "INFO"),
    "failed":    ("run_failed",    "WARNING"),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InvalidRunTransitionError(RuntimeError):
    """
    Raised when a state transition is not permitted by the run state machine.

    Attributes:
        run_id:     The run that was being transitioned.
        from_state: Current state of the run.
        to_state:   Attempted destination state.
    """
    def __init__(
        self,
        run_id: str,
        from_state: Optional[str],
        to_state: str,
    ) -> None:
        self.run_id = run_id
        self.from_state = from_state
        self.to_state = to_state
        valid = sorted(_VALID_TRANSITIONS.get(from_state, set()))
        super().__init__(
            f"Run {run_id!r}: invalid transition {from_state!r} → {to_state!r}. "
            f"Valid from {from_state!r}: {valid}"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir(runs_dir: Optional[Path]) -> Path:
    return runs_dir or _DEFAULT_RUNS_DIR


def _db_path(db_path: Optional[Path]) -> Path:
    return db_path or _DEFAULT_DB_PATH


def _model_assignments_from_config(config: Config) -> dict:
    """Extract current model assignments from config for snapshotting."""
    return {
        "ep":          getattr(config, "EP_MODEL",          "anthropic/claude-sonnet-4.6"),
        "rd":          getattr(config, "RD_MODEL",          "anthropic/claude-opus-4.6"),
        "td_planning": getattr(config, "TD_PLANNING_MODEL", "openai/gpt-5.5-pro"),
        "bga_pass1":   getattr(config, "BGA_PASS1_MODEL",   "anthropic/claude-sonnet-4.6"),
        "bga_pass2":   getattr(config, "BGA_PASS2_MODEL",   "openai/gpt-5.5-pro"),
    }


def _write_run_json(run_id: str, record: dict, runs_dir: Path) -> None:
    """Write individual run JSON file. Never raises — DB is the authoritative store."""
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{run_id}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception:
        pass


def _append_index(run_id: str, episode_id: str, status: str, runs_dir: Path) -> None:
    """Append one line to runs/index.jsonl. Never raises."""
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "run_id":      run_id,
            "episode_id":  episode_id,
            "status":      status,
            "recorded_at": _now_iso(),
        })
        with open(runs_dir / "index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except Exception:
        pass


def _record_transition(
    run_id: str,
    from_state: Optional[str],
    to_state: str,
    triggered_by: str,
    trigger_source: str,
    db_path: Optional[Path],
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> str:
    """
    Insert a run_state_transitions row and update runs.status (plus any
    state-specific column stamps).

    Internal — does NOT validate the transition. Callers must validate first.
    Returns the new transition_id.

    Column stamps by destination state:
        completed → completed_at, exit_code = 0
        failed    → completed_at, exit_code = 1, error_message (from reason)
        paused    → paused_at
    """
    transition_id = str(uuid.uuid4())
    transitioned_at = _now_iso()
    conn = get_connection(_db_path(db_path))
    try:
        insert_run_state_transition(
            conn,
            transition_id=transition_id,
            run_id=run_id,
            from_state=from_state,
            to_state=to_state,
            triggered_by=triggered_by,
            trigger_source=trigger_source,
            transitioned_at=transitioned_at,
            reason=reason,
            metadata=metadata,
        )
        conn.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (to_state, run_id),
        )
        if to_state == "completed":
            conn.execute(
                "UPDATE runs SET completed_at = ?, exit_code = ? WHERE run_id = ?",
                (transitioned_at, 0, run_id),
            )
        elif to_state == "failed":
            conn.execute(
                "UPDATE runs SET completed_at = ?, exit_code = 1, error_message = ? "
                "WHERE run_id = ?",
                (transitioned_at, (reason or "")[:4096], run_id),
            )
        elif to_state == "paused":
            conn.execute(
                "UPDATE runs SET paused_at = ? WHERE run_id = ?",
                (transitioned_at, run_id),
            )
        conn.commit()
    finally:
        conn.close()
    return transition_id


def _emit_ois_signal_nonblocking(
    to_state: str,
    run_id: str,
    episode_id: Optional[str],
    channel_id: Optional[str],
    db_path: Optional[Path],
) -> None:
    """Emit an OIS signal for notable state arrivals. Never raises."""
    signal_info = _TRANSITION_OIS_SIGNALS.get(to_state)
    if signal_info is None:
        return
    signal_type, severity = signal_info
    try:
        from hypogeum.ois import emit_signal
        emit_signal(
            signal_type=signal_type,
            severity=severity,
            message=f"Run {run_id!r} transitioned to {to_state!r}.",
            channel_id=channel_id,
            source_system="run_registry",
            blocking=False,
            episode_id=episode_id,
            run_id=run_id,
            db_path=db_path,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API — State machine
# ---------------------------------------------------------------------------

def get_run_state(
    run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Return the current status of a run from the runs table.

    Returns None if the run does not exist.
    """
    conn = get_connection(_db_path(db_path))
    try:
        row = conn.execute(
            "SELECT status FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def get_run_transition_history(
    run_id: str,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    Return an ordered list of all state transitions for a run.

    Each entry is a dict with: transition_id, run_id, from_state, to_state,
    triggered_by, trigger_source, reason, metadata, transitioned_at.
    """
    conn = get_connection(_db_path(db_path))
    try:
        rows = get_run_state_transitions(conn, run_id)
        return [dict(r) for r in rows]
    finally:
        conn.close()


def transition_run_state(
    run_id: str,
    to_state: str,
    triggered_by: str,
    trigger_source: str,
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
    db_path: Optional[Path] = None,
    episode_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> str:
    """
    Validate and apply a run state transition.

    Reads the current state from the runs table, checks it against
    _VALID_TRANSITIONS, writes an immutable run_state_transitions row,
    updates runs.status, and emits an OIS signal for completed/failed.

    Args:
        run_id:         UUID4 string identifying the run.
        to_state:       Target state (must be in RUN_STATES).
        triggered_by:   Actor initiating the transition (user_id or
                        "system:pipeline", "system:ois", etc.).
        trigger_source: Component name (e.g. "run_registry", "pipeline").
        reason:         Optional human-readable context.
        metadata:       Optional JSON-serialisable dict for extended context.
        db_path:        Override DB path (always pass in tests).
        episode_id:     Used for OIS signal emission (optional lookup).
        channel_id:     Used for OIS signal emission (optional).

    Returns:
        transition_id — UUID4 string of the new run_state_transitions row.

    Raises:
        InvalidRunTransitionError — transition is not valid from current state.
        ValueError                — to_state is not a recognised run state.
    """
    if to_state not in RUN_STATES:
        raise ValueError(
            f"Unknown run state {to_state!r}. Valid states: {sorted(RUN_STATES)}"
        )

    current = get_run_state(run_id, db_path=db_path)
    allowed = _VALID_TRANSITIONS.get(current, frozenset())
    if to_state not in allowed:
        raise InvalidRunTransitionError(run_id, current, to_state)

    tid = _record_transition(
        run_id=run_id,
        from_state=current,
        to_state=to_state,
        triggered_by=triggered_by,
        trigger_source=trigger_source,
        db_path=db_path,
        reason=reason,
        metadata=metadata,
    )

    _emit_ois_signal_nonblocking(
        to_state=to_state,
        run_id=run_id,
        episode_id=episode_id,
        channel_id=channel_id,
        db_path=db_path,
    )

    return tid


def pause_run(
    run_id: str,
    operator_user_id: str,
    reason: str,
    db_path: Optional[Path] = None,
) -> str:
    """
    Transition a running run to paused state.

    Sets runs.paused_at timestamp. Returns transition_id.
    Raises InvalidRunTransitionError if the run is not in 'running' state.
    """
    return transition_run_state(
        run_id=run_id,
        to_state="paused",
        triggered_by=operator_user_id,
        trigger_source="operator",
        reason=reason,
        db_path=db_path,
    )


def resume_run(
    run_id: str,
    operator_user_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """
    Resume a paused run, transitioning back to running.

    Returns transition_id.
    Raises InvalidRunTransitionError if the run is not in 'paused' state.
    """
    return transition_run_state(
        run_id=run_id,
        to_state="running",
        triggered_by=operator_user_id,
        trigger_source="operator",
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Public API — Lifecycle (façade wrappers over the state machine)
# ---------------------------------------------------------------------------

def register_run(
    episode_id: str,
    config: Config,
    db_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> str:
    """
    Register a new pipeline execution. Must be called before any agent runs.

    Creates:
      - A row in runs table with status='running'
      - An immutable run_state_transitions row for None → running
      - A JSON file at runs/{run_id}.json
      - An entry in runs/index.jsonl

    Args:
        episode_id:  Episode identifier (e.g. "ep_000").
        config:      Loaded Config instance. Used to snapshot model assignments.
        db_path:     Override SQLite path (tests use tmp_path).
        runs_dir:    Override JSON/index directory (tests use tmp_path).
        channel_id:  Optional channel identifier. Recorded in the run row.

    Returns:
        run_id: UUID4 string. Pass to complete_run or fail_run.
    """
    run_id = generate_run_id()
    started_at = _now_iso()
    model_assignments = _model_assignments_from_config(config)

    # Ensure schema exists (idempotent) — also applies column migrations
    initialize_schema(_db_path(db_path))

    # Write DB row (status='running' — pipeline starts immediately)
    conn = get_connection(_db_path(db_path))
    try:
        insert_run(
            conn,
            run_id=run_id,
            episode_id=episode_id,
            started_at=started_at,
            model_assignments=model_assignments,
            metadata_version=METADATA_VERSION,
        )
        if channel_id is not None:
            conn.execute(
                "UPDATE runs SET channel_id = ? WHERE run_id = ?",
                (channel_id, run_id),
            )
        conn.commit()
    finally:
        conn.close()

    # Record the initial state transition (None → running) — non-blocking
    try:
        _record_transition(
            run_id=run_id,
            from_state=None,
            to_state="running",
            triggered_by="system:pipeline",
            trigger_source="run_registry",
            db_path=db_path,
        )
    except Exception:
        pass  # transition record failure must not block pipeline start

    # Write JSON record
    record = {
        "run_id":           run_id,
        "episode_id":       episode_id,
        "started_at":       started_at,
        "status":           "running",
        "model_assignments": model_assignments,
        "metadata_version": METADATA_VERSION,
    }
    if channel_id is not None:
        record["channel_id"] = channel_id
    _write_run_json(run_id, record, _runs_dir(runs_dir))
    _append_index(run_id, episode_id, "running", _runs_dir(runs_dir))

    return run_id


def complete_run(
    run_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
) -> None:
    """
    Mark a run as successfully completed.

    Façade over transition_run_state("completed"). Also updates the JSON
    file and appends to index.jsonl for offline access.

    Args:
        run_id:     UUID4 string returned by register_run().
        episode_id: Episode identifier (for index entry).
        db_path:    Override SQLite path.
        runs_dir:   Override JSON/index directory.
    """
    completed_at = _now_iso()

    # State machine transition (handles DB status update + transition record + OIS)
    try:
        transition_run_state(
            run_id=run_id,
            to_state="completed",
            triggered_by="system:pipeline",
            trigger_source="run_registry",
            episode_id=episode_id,
            db_path=db_path,
        )
    except (InvalidRunTransitionError, Exception):
        # Fallback: write completed status directly if state machine fails
        # (e.g. run was already in a terminal state due to a race condition).
        conn = get_connection(_db_path(db_path))
        try:
            update_run_complete(conn, run_id, completed_at, exit_code=0)
            conn.commit()
        finally:
            conn.close()

    # Update JSON record
    rdir = _runs_dir(runs_dir)
    json_path = rdir / f"{run_id}.json"
    try:
        if json_path.exists():
            record = json.loads(json_path.read_text(encoding="utf-8"))
            record["status"] = "completed"
            record["completed_at"] = completed_at
            record["exit_code"] = 0
            _write_run_json(run_id, record, rdir)
    except Exception:
        pass

    _append_index(run_id, episode_id, "completed", rdir)


def fail_run(
    run_id: str,
    episode_id: str,
    error_message: str,
    db_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
) -> None:
    """
    Mark a run as failed. Call from the pipeline's except handler.

    Façade over transition_run_state("failed"). Also updates the JSON
    file and appends to index.jsonl.

    Args:
        run_id:        UUID4 string returned by register_run().
        episode_id:    Episode identifier (for index entry).
        error_message: Error description or traceback summary.
        db_path:       Override SQLite path.
        runs_dir:      Override JSON/index directory.
    """
    completed_at = _now_iso()

    # State machine transition — non-blocking if already terminal
    try:
        transition_run_state(
            run_id=run_id,
            to_state="failed",
            triggered_by="system:pipeline",
            trigger_source="run_registry",
            reason=error_message[:512],
            episode_id=episode_id,
            db_path=db_path,
        )
    except (InvalidRunTransitionError, Exception):
        # Fallback: write failed status directly
        conn = get_connection(_db_path(db_path))
        try:
            update_run_failed(conn, run_id, completed_at, error_message)
            conn.commit()
        finally:
            conn.close()

    # Update JSON record
    rdir = _runs_dir(runs_dir)
    json_path = rdir / f"{run_id}.json"
    try:
        if json_path.exists():
            record = json.loads(json_path.read_text(encoding="utf-8"))
            record["status"] = "failed"
            record["completed_at"] = completed_at
            record["exit_code"] = 1
            record["error_message"] = error_message[:4096]
            _write_run_json(run_id, record, rdir)
    except Exception:
        pass

    _append_index(run_id, episode_id, "failed", rdir)


def cancel_run(
    run_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
) -> None:
    """
    Mark a run as cancelled by operator request.

    Façade over transition_run_state("cancelled"). Also updates the JSON
    file and appends to index.jsonl.

    Args:
        run_id:     UUID4 string returned by register_run().
        episode_id: Episode identifier (for index entry).
        db_path:    Override SQLite path.
        runs_dir:   Override JSON/index directory.
    """
    cancelled_at = _now_iso()

    try:
        transition_run_state(
            run_id=run_id,
            to_state="cancelled",
            triggered_by="operator",
            trigger_source="worker_control",
            reason="operator_cancel_requested",
            episode_id=episode_id,
            db_path=db_path,
        )
    except (InvalidRunTransitionError, Exception):
        # Fallback: write cancelled status directly
        conn = get_connection(_db_path(db_path))
        try:
            conn.execute(
                "UPDATE runs SET status = 'cancelled', completed_at = ?, exit_code = 2 "
                "WHERE run_id = ?",
                (cancelled_at, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    rdir = _runs_dir(runs_dir)
    json_path = rdir / f"{run_id}.json"
    try:
        if json_path.exists():
            record = json.loads(json_path.read_text(encoding="utf-8"))
            record["status"] = "cancelled"
            record["completed_at"] = cancelled_at
            record["exit_code"] = 2
            _write_run_json(run_id, record, rdir)
    except Exception:
        pass

    _append_index(run_id, episode_id, "cancelled", rdir)
