"""
hypogeum/finance.py — Finance Controller v1

Per-call cost capture and per-episode rollup. Distinct from
hypogeum/agents/finance_agent.py (which is a budget estimation gate).
This module records actual API spend to the operational database.

Architecture:
    Pipeline calls set_run_context() at run start and clear_run_context()
    at run end. Any module can then call record_cost() without threading
    run_id through every function signature.

    record_cost()           — called after every billable API call
    write_episode_rollup()  — called at run completion; aggregates all costs

Public API:
    set_run_context(run_id, episode_id, db_path=None)  -> None
    clear_run_context()                                 -> None
    record_cost(provider, model, call_type, cost_usd,
                tokens_or_credits=None)                 -> None
    write_episode_rollup(run_id, episode_id,
                         db_path=None, finance_dir=None) -> dict

Coverage:
    OpenRouter (LLM calls) — wired via agent.py
    MUAPI (video/image generation) — wired via muapi.py
    ElevenLabs (narration/music) — wired via elevenlabs.py
    Perplexity (web search) — wired via perplexity.py

Finance controller writes are non-blocking. Any write failure is
silently swallowed — cost recording must never halt the pipeline.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, insert_cost


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_DEFAULT_FINANCE_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "finance"


# ---------------------------------------------------------------------------
# Module-level run context
# Holds the active run_id + episode_id so callers don't need to thread
# these values through every function signature.
# ---------------------------------------------------------------------------

_active_run: dict = {}


def set_run_context(
    run_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Set the active run context for cost recording.

    Must be called after register_run() and before any agent calls.
    Clears any previously active context.

    Args:
        run_id:     UUID4 string from register_run().
        episode_id: Episode identifier (e.g. "ep_000").
        db_path:    Override SQLite path (tests use tmp_path).
    """
    _active_run.clear()
    _active_run["run_id"] = run_id
    _active_run["episode_id"] = episode_id
    _active_run["db_path"] = db_path


def clear_run_context() -> None:
    """
    Clear the active run context.

    Call at run completion or in the pipeline's finally block to prevent
    cost records from leaking across runs in long-lived processes.
    """
    _active_run.clear()


def get_run_context() -> dict:
    """
    Return a copy of the current run context. Empty dict if not set.

    Read-only. Does not modify the active context.
    """
    return dict(_active_run)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_cost_id() -> str:
    return str(uuid.uuid4())


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    if db_path is not None:
        return db_path
    return _active_run.get("db_path") or _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Per-call cost recording
# ---------------------------------------------------------------------------

def record_cost(
    provider: str,
    model: Optional[str],
    call_type: str,
    cost_usd: float,
    tokens_or_credits: Optional[float] = None,
    asset_id: Optional[str] = None,
    run_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """
    Record one billable API call. Non-blocking — silently ignores all errors.

    Uses the active run context for run_id and episode_id if not provided
    explicitly. If neither source provides them, the call is silently skipped.

    Args:
        provider:           Provider slug ("openrouter", "muapi", "elevenlabs",
                            "perplexity").
        model:              Model slug (e.g. "anthropic/claude-opus-4.6"). None for
                            providers that do not report a model.
        call_type:          Semantic call category ("research_synthesis",
                            "scene_generation", "narration", "music",
                            "thumbnail_planning", "td_planning", "web_search").
        cost_usd:           Actual or estimated cost for this call in USD.
        tokens_or_credits:  Token count (LLM/Perplexity) or credit count (MUAPI)
                            or character count (ElevenLabs TTS).
        asset_id:           Asset that incurred this cost (nullable). Links cost
                            row to a specific generated asset for provenance.
        run_id:             Override run_id. Falls back to active run context.
        episode_id:         Override episode_id. Falls back to active run context.
        db_path:            Override SQLite path.
    """
    try:
        effective_run_id = run_id or _active_run.get("run_id")
        effective_episode_id = episode_id or _active_run.get("episode_id")

        if not effective_run_id or not effective_episode_id:
            return  # No active run context — skip silently

        conn = get_connection(_resolve_db_path(db_path))
        try:
            insert_cost(
                conn,
                cost_id=_new_cost_id(),
                run_id=effective_run_id,
                episode_id=effective_episode_id,
                provider=provider,
                call_type=call_type,
                cost_usd=cost_usd,
                called_at=_now_iso(),
                model=model,
                tokens_or_credits=tokens_or_credits,
                asset_id=asset_id,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Cost recording must never halt the pipeline


# ---------------------------------------------------------------------------
# Episode rollup
# ---------------------------------------------------------------------------

def write_episode_rollup(
    run_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
    finance_dir: Optional[Path] = None,
) -> dict:
    """
    Aggregate all cost records for a run into a finance summary.

    Reads from the costs table and writes a JSON rollup to
    .hypogeum-assets/finance/{episode_id}_finance.json.

    Args:
        run_id:       UUID4 string identifying the run.
        episode_id:   Episode identifier.
        db_path:      Override SQLite path.
        finance_dir:  Override finance JSON output directory.

    Returns:
        Rollup dict with total_cost_usd, cost_by_provider,
        cost_by_call_type, call_count, metadata_version.
        Returns empty rollup dict on any error.
    """
    resolved_db = db_path or _DEFAULT_DB_PATH
    resolved_dir = finance_dir or _DEFAULT_FINANCE_DIR

    try:
        conn = get_connection(resolved_db)
        try:
            rows = conn.execute(
                "SELECT provider, call_type, cost_usd FROM costs WHERE run_id=?",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()

        total_cost_usd = sum(r["cost_usd"] for r in rows)
        cost_by_provider: dict = {}
        cost_by_call_type: dict = {}

        for row in rows:
            prov = row["provider"]
            ctype = row["call_type"]
            cost_by_provider[prov] = round(
                cost_by_provider.get(prov, 0.0) + row["cost_usd"], 6
            )
            cost_by_call_type[ctype] = round(
                cost_by_call_type.get(ctype, 0.0) + row["cost_usd"], 6
            )

        rollup = {
            "run_id": run_id,
            "episode_id": episode_id,
            "total_cost_usd": round(total_cost_usd, 6),
            "cost_by_provider": cost_by_provider,
            "cost_by_call_type": cost_by_call_type,
            "call_count": len(rows),
            "recorded_at": _now_iso(),
            "metadata_version": "v1",
        }

        # Write JSON rollup
        try:
            resolved_dir.mkdir(parents=True, exist_ok=True)
            out_path = resolved_dir / f"{episode_id}_finance.json"
            out_path.write_text(json.dumps(rollup, indent=2), encoding="utf-8")
        except Exception:
            pass  # JSON write failure is non-fatal

        return rollup

    except Exception:
        return {
            "run_id": run_id,
            "episode_id": episode_id,
            "total_cost_usd": 0.0,
            "cost_by_provider": {},
            "cost_by_call_type": {},
            "call_count": 0,
            "error": "rollup_failed",
            "metadata_version": "v1",
        }
