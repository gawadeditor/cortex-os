"""
hypogeum/model_comparison.py — Model Output Comparison Engine (Step D)

Compares LLM prompt outputs across multiple provider/model pairs using
call_claude() directly — not run_agent(). This avoids schema validation,
disk writes, and episode_id coupling that are inappropriate for a comparison
context.

Also provides archive query for zero-cost provenance surfacing: reads
generation_prompt, model, provider, and cost_usd from archive_index rows
written by the M7.2 provenance pass. Returns an empty list (never None) when
no provenance data exists (e.g. pre-M7.2 episodes).

Public API:
    DEFAULT_LLM_MATRIX        — default (provider, model) list for LLM runs
    BENCHMARK_USER_MESSAGE    — fixed user message used for all benchmark runs
    run_model_comparison()    — execute a live LLM comparison, write to DB
    query_archive_for_comparison() — read archive provenance (zero-cost)
    get_model_comparison_result()  — fetch latest completed run from DB
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.db import (
    get_connection,
    insert_model_comparison_run,
    complete_model_comparison_run,
    get_latest_model_comparison,
)
from hypogeum.openrouter import (
    call_claude,
    CallResult,
    OpenRouterError,
)
from hypogeum.prompt_registry import get_prompt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fixed user message for LLM benchmark runs — same topic as ep_val_01 so
# operators can cross-reference against real episode outputs.
BENCHMARK_USER_MESSAGE: str = (
    "The Antikythera Mechanism: ancient Greece's computing wonder"
)

# Default LLM matrix — all go through OpenRouter.
# Matches config.py defaults: OPENROUTER_SONNET_MODEL_ID / OPENROUTER_OPUS_MODEL_ID
# and TD_PLANNING_MODEL.
DEFAULT_LLM_MATRIX: list[dict] = [
    {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
    {"provider": "openrouter", "model": "anthropic/claude-opus-4.6"},
    {"provider": "openrouter", "model": "openai/gpt-5.5-pro"},
]

# Maximum tokens to request per model in a comparison run.
_MAX_COMPARISON_TOKENS = 2048

# OpenRouter environment variable name (mirrors provider_comparison.py pattern).
_OPENROUTER_ENV_VAR = "OPENROUTER_API_KEY"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Credential resolution (OpenRouter only for LLM runs)
# ---------------------------------------------------------------------------

def _resolve_openrouter_credential(db_path: Path) -> tuple[Optional[str], str]:
    """
    Return (api_key, source) for OpenRouter.

    Tries active SecretProfile first, then falls back to the env var.
    Returns (None, 'env') when no credential is found.
    source is 'secret_profile' or 'env'.
    """
    # 1. Try active SecretProfile
    try:
        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(provider="openrouter", db_path=db_path)
        active = [p for p in profiles if p.status == "active"]
        if active:
            return active[0].credential_data, "secret_profile"
    except Exception:
        pass

    # 2. Env var fallback
    val = os.environ.get(_OPENROUTER_ENV_VAR, "").strip()
    if val:
        return val, "env"

    return None, "env"


# ---------------------------------------------------------------------------
# Single-model LLM execution
# ---------------------------------------------------------------------------

def _run_one_model(
    provider: str,
    model_slug: str,
    messages: list[dict],
    config: Config,
) -> dict:
    """
    Call call_claude() for one (provider, model) pair.

    Returns a ModelComparisonResult-shaped dict. Never raises — errors are
    captured in result["status"] = "error".
    """
    result: dict = {
        "provider":          provider,
        "model":             model_slug,
        "status":            "pending",
        "latency_ms":        None,
        "cost_usd":          None,
        "prompt_text":       None,
        "output_text":       None,
        "output_path":       None,
        "output_tokens":     None,
        "error":             None,
        "credential_source": None,
        "archive_asset_id":  None,
    }

    # Store the exact messages submitted.
    result["prompt_text"] = json.dumps(messages)

    t0 = time.monotonic()
    try:
        call_result: CallResult = call_claude(
            model_slug=model_slug,
            messages=messages,
            max_tokens=_MAX_COMPARISON_TOKENS,
            config=config,
            agent="model_comparison",
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        result["status"]        = "success"
        result["latency_ms"]    = elapsed_ms
        result["output_text"]   = call_result.content
        result["output_tokens"] = call_result.usage.get("completion_tokens")
        result["cost_usd"]      = call_result.estimated_cost_usd

    except OpenRouterError as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result["status"]     = "error"
        result["latency_ms"] = elapsed_ms
        result["error"]      = str(exc)

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result["status"]     = "error"
        result["latency_ms"] = elapsed_ms
        result["error"]      = f"{type(exc).__name__}: {exc}"

    return result


# ---------------------------------------------------------------------------
# Public: run_model_comparison
# ---------------------------------------------------------------------------

def run_model_comparison(
    prompt_text: str,
    initiated_at: str,
    db_path: Optional[Path] = None,
    run_id: Optional[str] = None,
    run_type: str = "llm",
    prompt_name: Optional[str] = None,
    user_message: str = BENCHMARK_USER_MESSAGE,
    provider_matrix: Optional[list[dict]] = None,
    operator_note: Optional[str] = None,
) -> dict:
    """
    Execute a live LLM model comparison.

    Calls call_claude() for each (provider, model) pair in provider_matrix
    with the given system prompt and a fixed user message. Writes the run
    and results to model_comparison_runs. Returns a dict summarising the run.

    Args:
        prompt_text:     System prompt body (headers already stripped by
                         get_prompt() or passed directly).
        initiated_at:    ISO timestamp when the operator approved the run.
        db_path:         Path to the SQLite DB. Defaults to the standard path.
        run_id:          UUID for this run. Auto-generated if None.
        run_type:        "llm" | "image" | "video". Only "llm" is implemented.
        prompt_name:     Registered prompt file stem (e.g. "executive_producer")
                         or None for unnamed prompts.
        user_message:    User turn content sent alongside the system prompt.
                         Defaults to BENCHMARK_USER_MESSAGE.
        provider_matrix: List of {"provider": ..., "model": ...} dicts.
                         Defaults to DEFAULT_LLM_MATRIX.
        operator_note:   Optional operator note stored with the run.

    Returns:
        dict with keys: run_id, run_type, status, initiated_at, completed_at,
        prompt_name, results (list of ModelComparisonResult dicts).
    """
    if db_path is None:
        db_path = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

    if run_id is None:
        run_id = str(uuid.uuid4())

    if provider_matrix is None:
        provider_matrix = DEFAULT_LLM_MATRIX

    matrix_json = json.dumps(provider_matrix)

    db_path = Path(db_path)
    conn = get_connection(db_path)
    try:
        insert_model_comparison_run(
            conn,
            run_id=run_id,
            run_type=run_type,
            prompt_text=prompt_text,
            provider_matrix=matrix_json,
            initiated_at=initiated_at,
            prompt_name=prompt_name,
            operator_note=operator_note,
        )
        conn.commit()
    finally:
        conn.close()

    # Resolve OpenRouter credential once for all LLM pairs.
    credential, cred_source = _resolve_openrouter_credential(db_path)

    results: list[dict] = []

    if run_type == "llm":
        messages = [
            {"role": "system", "content": prompt_text},
            {"role": "user",   "content": user_message},
        ]

        for pair in provider_matrix:
            provider  = pair.get("provider", "openrouter")
            model_slug = pair.get("model", "")

            if not credential:
                results.append({
                    "provider":          provider,
                    "model":             model_slug,
                    "status":            "skipped_no_key",
                    "latency_ms":        None,
                    "cost_usd":          None,
                    "prompt_text":       json.dumps(messages),
                    "output_text":       None,
                    "output_path":       None,
                    "output_tokens":     None,
                    "error":             f"No credential found (check {_OPENROUTER_ENV_VAR} or add a Secret Profile)",
                    "credential_source": "none",
                    "archive_asset_id":  None,
                })
                continue

            # Build a per-call Config with the resolved credential.
            cfg = copy.copy(Config())
            cfg.OPENROUTER_API_KEY = credential

            result = _run_one_model(provider, model_slug, messages, cfg)
            result["credential_source"] = cred_source
            results.append(result)
    else:
        # image / video not implemented in v1 — placeholder for future.
        results = [{
            "provider":         None,
            "model":            None,
            "status":           "error",
            "error":            f"run_type={run_type!r} is not implemented in v1 model_comparison",
            "latency_ms":       None,
            "cost_usd":         None,
            "prompt_text":      None,
            "output_text":      None,
            "output_path":      None,
            "output_tokens":    None,
            "credential_source": None,
            "archive_asset_id": None,
        }]

    completed_at = datetime.now(timezone.utc).isoformat()
    all_skipped  = all(r["status"] in ("skipped_no_key", "error") for r in results)
    final_status = "error" if all_skipped else "completed"

    conn = get_connection(db_path)
    try:
        complete_model_comparison_run(
            conn,
            run_id=run_id,
            results=results,
            completed_at=completed_at,
            status=final_status,
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "run_id":        run_id,
        "run_type":      run_type,
        "prompt_name":   prompt_name,
        "status":        final_status,
        "initiated_at":  initiated_at,
        "completed_at":  completed_at,
        "results":       results,
    }


# ---------------------------------------------------------------------------
# Public: query_archive_for_comparison
# ---------------------------------------------------------------------------

def query_archive_for_comparison(
    episode_id: str,
    conn,
) -> list[dict]:
    """
    Return archive_index rows with populated M7.2 provenance fields for
    the given episode. Zero-cost read — no generation, no API calls.

    Rows where generation_prompt IS NULL are excluded; such rows exist for
    episodes generated before the M7.2 provenance pass. When no rows match
    (e.g. ep_val_01 or any pre-M7.2 episode) an empty list is returned — the
    caller is responsible for displaying a "No provenance data" message.

    Args:
        episode_id: Episode identifier (e.g. "ep_val_01").
        conn:       Open sqlite3.Connection — caller manages lifecycle.

    Returns:
        List of dicts with keys: asset_id, provider, model, generation_prompt,
        generation_params, cost_usd, archive_location, asset_created_at, scene_id.
        Empty list when no provenance data is available.
    """
    rows = conn.execute(
        """
        SELECT
            a.asset_id,
            a.provider,
            a.model,
            a.generation_prompt,
            a.generation_params,
            a.cost_usd,
            a.archive_location,
            a.asset_created_at,
            a.scene_id
        FROM archive_index a
        WHERE a.episode_id = ?
          AND a.generation_prompt IS NOT NULL
          AND a.provider IS NOT NULL
        ORDER BY a.asset_created_at DESC
        """,
        (episode_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Public: get_model_comparison_result
# ---------------------------------------------------------------------------

def get_model_comparison_result(
    db_path: Optional[Path] = None,
    run_type: str = "llm",
) -> Optional[dict]:
    """
    Return the most recent completed model comparison run of the given type.

    Thin wrapper around get_latest_model_comparison() from db.py.
    Returns None if no completed run exists.

    Args:
        db_path:  Path to the SQLite DB. Defaults to the standard path.
        run_type: "llm" | "image" | "video".
    """
    if db_path is None:
        db_path = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

    conn = get_connection(Path(db_path))
    try:
        return get_latest_model_comparison(conn, run_type=run_type)
    finally:
        conn.close()
