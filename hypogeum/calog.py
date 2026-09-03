"""
hypogeum/calog.py — Channel Activation & Lifecycle Governance (CALOG)

Single authority on channel state. Defines what state a channel is in,
what transitions are valid, and what conditions must be met before a
channel may become active.

CALOG does not execute pipelines, select providers, generate content,
approve content, or publish. It governs channel operating state only.

Channel states:
    draft       — channel record exists; profiles not yet linked
    configured  — all three sub-profiles linked; not yet activated
    active      — all activation gates passed; scheduler may enqueue runs
    paused      — operator-suspended; no new runs; can resume to active
    blocked     — OIS blocking signal present; no new runs
    archived    — permanently retired; only error transition permitted
    error       — unexpected condition; operator resets to draft

Valid transitions:
    None        → draft, configured, active, error   (first transition)
    draft       → configured, active, error
    configured  → active, archived, error
    active      → paused, blocked, archived, error
    paused      → active, blocked, archived, error
    blocked     → paused, archived, error
    archived    → error
    error       → draft, configured, active

Activation gates (run in order; all must pass for active state):
    Gate 1 — Channel exists in DB
    Gate 2 — Profile complete (brand + model + publishing all linked)
    Gate 3 — At least one active credential for a core provider
    Gate 4 — Budget ceiling defined (> 0)
    Gate 5 — No active blocking OIS signal for channel or factory

Public API:
    get_channel_state(channel_id, db_path=None)          -> str
    set_channel_state(channel_id, to_state, ...)         -> str  (transition_id)
    activate_channel(channel_id, config=None, ...)       -> ActivationResult
    pause_channel(channel_id, reason=None, ...)          -> str
    block_channel(channel_id, reason=None, ...)          -> str
    archive_channel(channel_id, reason=None, ...)        -> str
    is_channel_runnable(channel_id, db_path=None)        -> bool
    get_transition_history(channel_id, db_path=None)     -> list[dict]
    run_activation_check(channel_id, config=None, ...)   -> ActivationResult
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import (
    get_channel_transition_history,
    get_connection,
    get_latest_channel_state,
    initialize_schema,
    insert_channel_state_transition,
)


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

# Core providers that must have at least one active credential for Gate 3
_REQUIRED_PROVIDERS = {"openrouter", "muapi", "elevenlabs"}

# Valid state values
STATES = frozenset({
    "draft", "configured", "active", "paused", "blocked", "archived", "error"
})

# Allowed transitions: current_state -> set of reachable states
# None represents "no prior transition" (first-time state assignment)
_VALID_TRANSITIONS: dict[Optional[str], frozenset] = {
    None:         frozenset({"draft", "configured", "active", "error"}),
    "draft":      frozenset({"configured", "active", "error"}),
    "configured": frozenset({"active", "archived", "error"}),
    "active":     frozenset({"paused", "blocked", "archived", "error"}),
    "paused":     frozenset({"active", "blocked", "archived", "error"}),
    "blocked":    frozenset({"paused", "archived", "error"}),
    "archived":   frozenset({"error"}),
    "error":      frozenset({"draft", "configured", "active"}),
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    gate: str
    passed: bool
    reason: str


@dataclass
class ActivationResult:
    success: bool
    new_state: str
    gate_results: list[GateResult] = field(default_factory=list)
    transition_id: Optional[str] = None
    failure_reason: Optional[str] = None


class CALOGTransitionError(ValueError):
    """Raised when a requested state transition is not permitted."""
    def __init__(self, channel_id: str, from_state: Optional[str], to_state: str):
        self.channel_id = channel_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Channel '{channel_id}': transition {from_state!r} → {to_state!r} is not permitted."
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _assert_valid_transition(
    channel_id: str,
    from_state: Optional[str],
    to_state: str,
) -> None:
    allowed = _VALID_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise CALOGTransitionError(channel_id, from_state, to_state)


# ---------------------------------------------------------------------------
# Activation gate checks
# ---------------------------------------------------------------------------

def _gate_channel_exists(channel_id: str, db_path: Path) -> GateResult:
    """Gate 1: channel profile row must exist."""
    try:
        from hypogeum.channel_config import get_channel_profile
        profile = get_channel_profile(channel_id, db_path=db_path)
        if profile is not None:
            return GateResult("channel_exists", True, "Channel profile found.")
        return GateResult("channel_exists", False, f"No channel profile for '{channel_id}'.")
    except Exception as exc:
        return GateResult("channel_exists", False, f"Error checking channel: {exc}")


def _gate_profile_complete(channel_id: str, db_path: Path) -> GateResult:
    """Gate 2: brand, model, and publishing profiles must all be linked."""
    try:
        from hypogeum.channel_config import get_channel_profile
        profile = get_channel_profile(channel_id, db_path=db_path)
        if profile is None:
            return GateResult("profile_complete", False, "Channel profile not found.")
        missing = []
        if not profile.brand_profile_id:
            missing.append("brand_profile_id")
        if not profile.model_profile_id:
            missing.append("model_profile_id")
        if not profile.publishing_profile_id:
            missing.append("publishing_profile_id")
        if missing:
            return GateResult(
                "profile_complete", False,
                f"Missing profile links: {', '.join(missing)}.",
            )
        return GateResult("profile_complete", True, "All sub-profiles linked.")
    except Exception as exc:
        return GateResult("profile_complete", False, f"Error checking profiles: {exc}")


def _gate_credentials(channel_id: str, db_path: Path) -> GateResult:
    """Gate 3: at least one active credential for a core provider."""
    try:
        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(db_path=db_path)
        active_providers = {
            p.provider for p in profiles
            if p.status == "active" and p.provider in _REQUIRED_PROVIDERS
        }
        if active_providers:
            return GateResult(
                "credentials", True,
                f"Active credentials for: {', '.join(sorted(active_providers))}.",
            )
        return GateResult(
            "credentials", False,
            f"No active credentials found for required providers: "
            f"{', '.join(sorted(_REQUIRED_PROVIDERS))}.",
        )
    except Exception as exc:
        return GateResult("credentials", False, f"Error checking credentials: {exc}")


def _gate_budget(config) -> GateResult:
    """Gate 4: budget ceiling must be > 0."""
    if config is None:
        # No config provided — try to load from environment
        try:
            from hypogeum.config import Config
            config = Config()
        except Exception as exc:
            return GateResult("budget", False, f"Could not load config: {exc}")

    ceiling = getattr(config, "FINANCE_BUDGET_CEILING_USD", 0.0) or 0.0
    if ceiling > 0:
        return GateResult("budget", True, f"Per-episode spend limit set: ${ceiling:.2f}.")
    return GateResult(
        "budget", False,
        "No per-episode spend limit set. Define one before activating this channel.",
    )


def _gate_ois_clear(channel_id: str, db_path: Path) -> GateResult:
    """Gate 5: no active blocking OIS signal for this channel or the factory."""
    try:
        from hypogeum.ois import is_channel_blocked
        if is_channel_blocked(channel_id, db_path=db_path):
            return GateResult(
                "ois_clear", False,
                f"Active blocking OIS signal exists for channel '{channel_id}' or factory.",
            )
        return GateResult("ois_clear", True, "No blocking OIS signals active.")
    except Exception as exc:
        return GateResult("ois_clear", False, f"Error checking OIS: {exc}")


def _run_gates(
    channel_id: str,
    db_path: Path,
    config=None,
) -> list[GateResult]:
    return [
        _gate_channel_exists(channel_id, db_path),
        _gate_profile_complete(channel_id, db_path),
        _gate_credentials(channel_id, db_path),
        _gate_budget(config),
        _gate_ois_clear(channel_id, db_path),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_channel_state(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """
    Return the current state of a channel.

    If no transition record exists (channel was never transitioned through
    CALOG), returns "draft" as the default initial state.

    Args:
        channel_id: Channel identifier.
        db_path:    Override DB path (for tests).

    Returns:
        One of: draft, configured, active, paused, blocked, archived, error.
    """
    resolved = _resolve_db_path(db_path)
    initialize_schema(resolved)
    conn = get_connection(resolved)
    try:
        state = get_latest_channel_state(conn, channel_id)
    finally:
        conn.close()
    return state if state is not None else "draft"


def set_channel_state(
    channel_id: str,
    to_state: str,
    triggered_by: str = "operator",
    reason: Optional[str] = None,
    gate_results: Optional[list[GateResult]] = None,
    db_path: Optional[Path] = None,
) -> str:
    """
    Transition a channel to a new state, recording the change immutably.

    Validates the transition against the state machine before writing.
    Raises CALOGTransitionError if the transition is not permitted.

    Args:
        channel_id:   Channel identifier.
        to_state:     Target state (must be in STATES).
        triggered_by: Who/what triggered the transition.
        reason:       Human-readable reason for the transition.
        gate_results: Gate check results to store (activation only).
        db_path:      Override DB path (for tests).

    Returns:
        transition_id (UUID4 string) of the created record.

    Raises:
        CALOGTransitionError: If the transition is not valid from the current state.
        ValueError: If to_state is not a known state value.
    """
    if to_state not in STATES:
        raise ValueError(f"Unknown state: {to_state!r}. Valid states: {sorted(STATES)}")

    resolved = _resolve_db_path(db_path)
    initialize_schema(resolved)

    conn = get_connection(resolved)
    try:
        from_state = get_latest_channel_state(conn, channel_id)
        _assert_valid_transition(channel_id, from_state, to_state)

        transition_id = _new_id()
        now = _now_iso()
        gate_json = (
            json.dumps([
                {"gate": g.gate, "passed": g.passed, "reason": g.reason}
                for g in gate_results
            ])
            if gate_results
            else None
        )

        insert_channel_state_transition(
            conn,
            transition_id=transition_id,
            channel_id=channel_id,
            from_state=from_state,
            to_state=to_state,
            triggered_by=triggered_by,
            reason=reason,
            gate_results=gate_json,
            transitioned_at=now,
        )
        conn.commit()
    finally:
        conn.close()

    return transition_id


def run_activation_check(
    channel_id: str,
    config=None,
    db_path: Optional[Path] = None,
) -> ActivationResult:
    """
    Run all activation gates without changing channel state.

    Safe to call from a dashboard or readiness check endpoint.

    Args:
        channel_id: Channel identifier.
        config:     Config object for budget gate. If None, loads from environment.
        db_path:    Override DB path (for tests).

    Returns:
        ActivationResult with success=True if all gates pass.
    """
    resolved = _resolve_db_path(db_path)
    current = get_channel_state(channel_id, db_path=resolved)
    gates = _run_gates(channel_id, resolved, config)
    all_pass = all(g.passed for g in gates)
    failed = next((g for g in gates if not g.passed), None)
    return ActivationResult(
        success=all_pass,
        new_state=current,
        gate_results=gates,
        transition_id=None,
        failure_reason=None if all_pass else (failed.reason if failed else None),
    )


def activate_channel(
    channel_id: str,
    config=None,
    db_path: Optional[Path] = None,
) -> ActivationResult:
    """
    Run activation gates and, if all pass, transition channel to 'active'.

    Current state must allow transition to 'active' (draft, configured, or error).
    If any gate fails, state is unchanged and ActivationResult.success is False.

    Args:
        channel_id: Channel identifier.
        config:     Config object for budget gate. If None, loads from environment.
        db_path:    Override DB path (for tests).

    Returns:
        ActivationResult indicating success, gate outcomes, and transition_id.
    """
    resolved = _resolve_db_path(db_path)
    current = get_channel_state(channel_id, db_path=resolved)

    # Validate the transition is structurally possible before running gates
    allowed = _VALID_TRANSITIONS.get(current if current != "draft" else None, frozenset())
    # Special handling: "draft" maps to key None for the first-transition case,
    # but get_channel_state returns "draft" as default. Normalise.
    from_key = None if current == "draft" and _get_raw_state(channel_id, resolved) is None else current
    allowed = _VALID_TRANSITIONS.get(from_key, frozenset())
    if "active" not in allowed:
        return ActivationResult(
            success=False,
            new_state=current,
            gate_results=[],
            failure_reason=(
                f"Transition from '{current}' to 'active' is not permitted. "
                f"Allowed transitions: {sorted(allowed)}."
            ),
        )

    gates = _run_gates(channel_id, resolved, config)
    all_pass = all(g.passed for g in gates)

    if not all_pass:
        failed = next(g for g in gates if not g.passed)
        return ActivationResult(
            success=False,
            new_state=current,
            gate_results=gates,
            failure_reason=f"Gate '{failed.gate}' failed: {failed.reason}",
        )

    # All gates pass — record transition
    # Use from_key to preserve correct from_state in the transition record
    conn = get_connection(resolved)
    try:
        raw_from = get_latest_channel_state(conn, channel_id)
    finally:
        conn.close()

    transition_id = set_channel_state(
        channel_id,
        to_state="active",
        triggered_by="system:calog",
        reason="All activation gates passed.",
        gate_results=gates,
        db_path=resolved,
    )

    return ActivationResult(
        success=True,
        new_state="active",
        gate_results=gates,
        transition_id=transition_id,
    )


def _get_raw_state(channel_id: str, db_path: Path) -> Optional[str]:
    """Return raw DB state (None if no transitions recorded)."""
    conn = get_connection(db_path)
    try:
        return get_latest_channel_state(conn, channel_id)
    finally:
        conn.close()


def pause_channel(
    channel_id: str,
    reason: Optional[str] = None,
    triggered_by: str = "operator",
    db_path: Optional[Path] = None,
) -> str:
    """
    Pause an active channel. Only valid from 'active' state.

    Args:
        channel_id:   Channel identifier.
        reason:       Why the channel is being paused.
        triggered_by: Who initiated the pause.
        db_path:      Override DB path (for tests).

    Returns:
        transition_id of the recorded transition.

    Raises:
        CALOGTransitionError: If the channel is not in 'active' state.
    """
    return set_channel_state(
        channel_id,
        to_state="paused",
        triggered_by=triggered_by,
        reason=reason,
        db_path=db_path,
    )


def block_channel(
    channel_id: str,
    reason: Optional[str] = None,
    triggered_by: str = "system:ois",
    db_path: Optional[Path] = None,
) -> str:
    """
    Block a channel due to an OIS blocking signal.

    Valid from 'active' or 'paused' states. Called by OIS when a blocking
    signal is emitted for this channel.

    Args:
        channel_id:   Channel identifier.
        reason:       Signal type or description that caused the block.
        triggered_by: Typically "system:ois".
        db_path:      Override DB path (for tests).

    Returns:
        transition_id of the recorded transition.

    Raises:
        CALOGTransitionError: If the channel is not in 'active' or 'paused' state.
    """
    return set_channel_state(
        channel_id,
        to_state="blocked",
        triggered_by=triggered_by,
        reason=reason,
        db_path=db_path,
    )


def archive_channel(
    channel_id: str,
    reason: Optional[str] = None,
    triggered_by: str = "operator",
    db_path: Optional[Path] = None,
) -> str:
    """
    Archive a channel permanently. Valid from configured, active, paused, or blocked.

    Args:
        channel_id:   Channel identifier.
        reason:       Why the channel is being archived.
        triggered_by: Who initiated the archive.
        db_path:      Override DB path (for tests).

    Returns:
        transition_id of the recorded transition.

    Raises:
        CALOGTransitionError: If the current state does not permit archiving.
    """
    return set_channel_state(
        channel_id,
        to_state="archived",
        triggered_by=triggered_by,
        reason=reason,
        db_path=db_path,
    )


def is_channel_runnable(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Return True if and only if the channel may accept a new pipeline run.

    Safe read interface for the Queue Consumer. A channel is runnable when:
      1. CALOG state is 'active'
      2. No active blocking OIS signal exists for the channel or factory

    The Queue Consumer must call this before claiming a queue item. If this
    returns False, the item must be skipped or re-queued.

    Args:
        channel_id: Channel identifier.
        db_path:    Override DB path (for tests).

    Returns:
        True if the channel is in 'active' state and OIS is clear.
    """
    state = get_channel_state(channel_id, db_path=db_path)
    if state != "active":
        return False
    try:
        from hypogeum.ois import is_channel_blocked
        resolved = _resolve_db_path(db_path)
        return not is_channel_blocked(channel_id, db_path=resolved)
    except Exception:
        # If OIS check fails, err on the side of caution
        return False


def get_transition_history(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    Return all state transitions for a channel, oldest first.

    Args:
        channel_id: Channel identifier.
        db_path:    Override DB path (for tests).

    Returns:
        List of dicts with keys: transition_id, channel_id, from_state,
        to_state, triggered_by, reason, gate_results, transitioned_at.
    """
    resolved = _resolve_db_path(db_path)
    initialize_schema(resolved)
    conn = get_connection(resolved)
    try:
        rows = get_channel_transition_history(conn, channel_id)
        return [dict(r) for r in rows]
    finally:
        conn.close()
