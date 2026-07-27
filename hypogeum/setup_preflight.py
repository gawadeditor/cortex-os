"""
hypogeum/setup_preflight.py — Factory Setup / Production Readiness Preflight

Unlike acceptance_audit.py (which only checks module imports), this checks
what is actually required to launch a real episode:
  - Required env vars present
  - ffmpeg on PATH
  - FINANCE_BUDGET_CEILING_USD configured and above estimated episode cost
  - Active OIS blocking signals
  - At least one active channel in DB

Public API:
    get_setup_preflight_status(db_path=None) -> dict
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_REQUIRED_ENV_VARS = [
    "OPENROUTER_API_KEY",
    "PERPLEXITY_API_KEY",
    "MUAPI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
]

# Estimated episode cost range (from finance_agent.py constants + Seedance benchmark)
_ESTIMATED_COST_LOW  = 18.0   # economy quality, Runway video
_ESTIMATED_COST_HIGH = 30.0   # premium quality, Seedance video


@dataclass
class SetupCheck:
    name:    str
    tier:    str    # "blocker" | "warning" | "info"
    passed:  bool
    message: str
    action:  str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_env_vars() -> tuple[list[SetupCheck], dict[str, bool]]:
    checks: list[SetupCheck] = []
    env_vars: dict[str, bool] = {}
    for var in _REQUIRED_ENV_VARS:
        present = bool(os.environ.get(var, "").strip())
        env_vars[var] = present
        checks.append(SetupCheck(
            name=var,
            tier="blocker",
            passed=present,
            message="Present" if present else f"Missing",
            action="" if present else f"export {var}=<your key>",
        ))
    return checks, env_vars


def _check_ffmpeg() -> SetupCheck:
    found = shutil.which("ffmpeg") is not None
    return SetupCheck(
        name="ffmpeg",
        tier="blocker",
        passed=found,
        message="Found on PATH" if found else "Not found",
        action="" if found else "brew install ffmpeg  (or: apt-get install ffmpeg)",
    )


def _check_budget_ceiling() -> tuple[SetupCheck, float]:
    try:
        raw = os.environ.get("FINANCE_BUDGET_CEILING_USD", "").strip()
        ceiling = float(raw) if raw else 0.0
    except (ValueError, TypeError):
        ceiling = 0.0

    if ceiling <= 0:
        return SetupCheck(
            name="budget_ceiling",
            tier="blocker",
            passed=False,
            message="Not configured (FINANCE_BUDGET_CEILING_USD=0 or unset). Pipeline gates will BLOCK every run.",
            action=f"export FINANCE_BUDGET_CEILING_USD={_ESTIMATED_COST_HIGH:.0f}",
        ), 0.0

    return SetupCheck(
        name="budget_ceiling",
        tier="info",
        passed=True,
        message=f"${ceiling:.2f} per episode",
    ), ceiling


def _check_cost_vs_ceiling(ceiling: float) -> SetupCheck:
    if ceiling <= 0:
        return SetupCheck(
            name="cost_vs_ceiling",
            tier="warning",
            passed=False,
            message=(
                f"No ceiling set. Typical episode cost: "
                f"${_ESTIMATED_COST_LOW:.0f}–${_ESTIMATED_COST_HIGH:.0f}"
            ),
            action=f"Set FINANCE_BUDGET_CEILING_USD first",
        )
    if ceiling < _ESTIMATED_COST_LOW:
        return SetupCheck(
            name="cost_vs_ceiling",
            tier="blocker",
            passed=False,
            message=(
                f"Ceiling ${ceiling:.2f} is BELOW estimated episode cost "
                f"${_ESTIMATED_COST_LOW:.0f}–${_ESTIMATED_COST_HIGH:.0f}. "
                f"Finance gate will raise FinancePostVPDBlockedError every run."
            ),
            action=(
                f"export FINANCE_BUDGET_CEILING_USD={_ESTIMATED_COST_HIGH:.0f}"
            ),
        )
    if ceiling < _ESTIMATED_COST_HIGH:
        return SetupCheck(
            name="cost_vs_ceiling",
            tier="warning",
            passed=True,
            message=(
                f"Ceiling ${ceiling:.2f} covers economy runs (≥${_ESTIMATED_COST_LOW:.0f}) "
                f"but may be exceeded on premium quality (up to ${_ESTIMATED_COST_HIGH:.0f})."
            ),
            action=f"Consider raising to ${_ESTIMATED_COST_HIGH:.0f} for premium runs",
        )
    return SetupCheck(
        name="cost_vs_ceiling",
        tier="info",
        passed=True,
        message=(
            f"Ceiling ${ceiling:.2f} covers estimated cost "
            f"${_ESTIMATED_COST_LOW:.0f}–${_ESTIMATED_COST_HIGH:.0f}"
        ),
    )


def _check_ois_signals(db_path: Optional[Path]) -> tuple[list[SetupCheck], list[dict]]:
    try:
        from hypogeum.ois import get_blocking_signals
        blocking = get_blocking_signals(db_path=db_path)
    except Exception as exc:
        return [SetupCheck(
            name="ois_check",
            tier="warning",
            passed=True,
            message=f"OIS check skipped: {exc}",
        )], []

    if not blocking:
        return [SetupCheck(
            name="ois_blocking_signals",
            tier="info",
            passed=True,
            message="No blocking OIS signals",
        )], []

    checks: list[SetupCheck] = []
    raw: list[dict] = []
    for sig in blocking:
        checks.append(SetupCheck(
            name=f"ois:{sig.signal_type}",
            tier="blocker",
            passed=False,
            message=f"[{sig.severity}] {sig.message}",
            action=(
                f"Resolve signal {sig.signal_id[:8]}… at /signals "
                f"or POST /api/action/resolve-signal/{sig.signal_id}"
            ),
        ))
        raw.append({
            "signal_id":   sig.signal_id,
            "signal_type": sig.signal_type,
            "severity":    sig.severity,
            "message":     sig.message,
            "created_at":  sig.created_at,
        })
    return checks, raw


def _check_active_channel(db_path: Optional[Path]) -> tuple[SetupCheck, int]:
    try:
        from hypogeum.db import get_connection, initialize_schema
        db = db_path or _DEFAULT_DB_PATH
        initialize_schema(db)
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM channel_profiles WHERE status='active'"
            ).fetchone()
            count = row["n"] if row else 0
        finally:
            conn.close()
    except Exception as exc:
        return SetupCheck(
            name="active_channel",
            tier="blocker",
            passed=False,
            message=f"Cannot check channels: {exc}",
            action="Verify DB at /channels",
        ), 0

    if count == 0:
        return SetupCheck(
            name="active_channel",
            tier="blocker",
            passed=False,
            message="No active channels found",
            action="Go to /channels and create or activate a channel",
        ), 0

    return SetupCheck(
        name="active_channel",
        tier="info",
        passed=True,
        message=f"{count} active channel{'s' if count != 1 else ''}",
    ), count


def get_setup_preflight_status(db_path: Optional[Path] = None) -> dict:
    """
    Run production-readiness preflight. Returns a dict suitable for templates
    and the /api/setup/preflight JSON endpoint.

    All checks are wrapped so this never raises.
    """
    all_checks: list[SetupCheck] = []

    env_checks, env_vars = _check_env_vars()
    all_checks.extend(env_checks)

    all_checks.append(_check_ffmpeg())

    ceiling_check, ceiling = _check_budget_ceiling()
    all_checks.append(ceiling_check)
    all_checks.append(_check_cost_vs_ceiling(ceiling))

    ois_checks, active_signals = _check_ois_signals(db_path)
    all_checks.extend(ois_checks)

    chan_check, chan_count = _check_active_channel(db_path)
    all_checks.append(chan_check)

    blockers = [c for c in all_checks if not c.passed and c.tier == "blocker"]
    warnings = [c for c in all_checks if not c.passed and c.tier == "warning"]

    return {
        "ready":               len(blockers) == 0,
        "checked_at":          _now_iso(),
        "blocker_count":       len(blockers),
        "warning_count":       len(warnings),
        "checks": [
            {
                "name":    c.name,
                "tier":    c.tier,
                "passed":  c.passed,
                "message": c.message,
                "action":  c.action,
            }
            for c in all_checks
        ],
        "blockers": [
            {"name": c.name, "message": c.message, "action": c.action}
            for c in blockers
        ],
        "warnings": [
            {"name": c.name, "message": c.message, "action": c.action}
            for c in warnings
        ],
        "env_vars":              env_vars,
        "budget_ceiling_usd":    ceiling,
        "estimated_cost_low":    _ESTIMATED_COST_LOW,
        "estimated_cost_high":   _ESTIMATED_COST_HIGH,
        "ffmpeg_found":          shutil.which("ffmpeg") is not None,
        "active_signals":        active_signals,
        "active_channel_count":  chan_count,
    }
