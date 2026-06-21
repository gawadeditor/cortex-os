"""
hypogeum/provider_comparison.py — Phase 1G: Provider Comparison Engine

Runs the same thumbnail generation prompt through multiple image providers
(openai, openrouter, muapi) and records results in the Factory OS DB.

Credential resolution order (per provider):
  1. Active SecretProfile in the DB for that provider
  2. Environment variable fallback (OPENAI_API_KEY, OPENROUTER_API_KEY, MUAPI_API_KEY)

Public API:
    run_provider_comparison(db_path=None) -> ComparisonRun

    get_comparison_result(db_path=None) -> Optional[dict]
      Returns the most recent completed comparison run from DB.
"""

from __future__ import annotations

import copy
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Module-level imports of generation functions so tests can patch them here.
# Lazy import inside _run_one_provider would bind names only in function scope,
# making patch("hypogeum.provider_comparison.<name>") ineffective.
from hypogeum.muapi import (
    MUAPIAuthFailure,
    MUAPIError,
    MUAPIQuotaExhausted,
    _generate_thumbnail_openai,
    _generate_thumbnail_openrouter,
    _attempt_thumbnail_generation,
    _normalize_thumbnail,
    _apply_wordmark_overlay,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

# Env var that backs each image provider credential
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openai":     "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "muapi":      "MUAPI_API_KEY",
}

# Estimated cost in USD per generation (approximate)
_PROVIDER_EST_COST: dict[str, float] = {
    "openai":     0.04,   # gpt-image-1 high quality
    "openrouter": 0.04,   # openai/gpt-5-image-mini via OpenRouter
    "muapi":      0.09,   # gpt-image-2-text-to-image via MUAPI proxy
}

# Fixed benchmark prompt — identical for every provider in every run.
# A representative Hypogeum Chronicles main-episode thumbnail scene.
COMPARISON_PROMPT = (
    "Cinematic aerial view of ancient Karnak temple complex at dusk, "
    "towering hypostyle hall columns casting long shadows, warm amber torchlight "
    "flickering between stone pillars, deep indigo sky above, "
    "subtle gold dust particles suspended in air, foreground empty — "
    "leaving space for text overlay. Mood: reverent, epic, archaeological wonder. "
    "Colour palette: void black shadows, antique gold light, parchment stone. "
    "Style: high-contrast photorealistic, no people, no modern elements, "
    "no watermarks, no borders, cinematic aspect ratio."
)

COMPARISON_ASPECT_RATIO = "16:9"
COMPARISON_DELIVERABLE_ID = "comparison"


@dataclass
class ComparisonRun:
    run_id: str
    run_at: str
    prompt: str
    results: list = field(default_factory=list)
    status: str = "completed"
    completed_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_credential(provider: str, db_path: Path) -> tuple[Optional[str], str]:
    """
    Return (credential, source) for a provider.

    source is 'secret_profile' or 'env'.
    Returns (None, 'env') when no credential is found.
    """
    # 1. Try active SecretProfile first
    try:
        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(provider=provider, db_path=db_path)
        active = [p for p in profiles if p.status == "active"]
        if active:
            return active[0].credential_data, "secret_profile"
    except Exception:
        pass

    # 2. Env var fallback
    env_var = _PROVIDER_ENV_VAR.get(provider, "")
    if env_var:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val, "env"

    return None, "env"


def _output_dir(run_id: str) -> Path:
    return _PROJECT_ROOT / ".hypogeum-assets" / "provider_comparisons" / run_id


def _run_one_provider(
    provider: str,
    run_id: str,
    base_config,
    db_path: Path,
) -> dict:
    """
    Attempt thumbnail generation for one provider.

    Returns a result dict with:
      provider, model, status, latency_ms, estimated_cost_usd,
      output_path, error, credential_source
    """
    from hypogeum.logger import get_logger

    out_dir = _output_dir(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    local_path = out_dir / f"thumb_{provider}.jpg"
    log = get_logger(episode_id=f"comparison_{run_id[:8]}", agent=f"cmp_{provider}")

    result: dict = {
        "provider":           provider,
        "model":              None,
        "status":             "pending",
        "latency_ms":         None,
        "estimated_cost_usd": _PROVIDER_EST_COST.get(provider),
        "output_path":        str(local_path),
        "error":              None,
        "credential_source":  None,
    }

    # Resolve credential
    credential, source = _resolve_credential(provider, db_path)
    result["credential_source"] = source

    if not credential:
        result["status"] = "skipped_no_key"
        result["error"]  = f"No credential found (check {_PROVIDER_ENV_VAR.get(provider, '?')} or add a Secret Profile)"
        return result

    # Build a config copy with the resolved credential injected
    cfg = copy.copy(base_config)
    if provider == "openai":
        cfg.OPENAI_API_KEY = credential
        result["model"] = cfg.OPENAI_IMAGE_MODEL
    elif provider == "openrouter":
        cfg.OPENROUTER_API_KEY = credential
        result["model"] = cfg.OPENROUTER_IMAGE_MODEL
    elif provider == "muapi":
        cfg.MUAPI_API_KEY = credential
        result["model"] = cfg.THUMBNAIL_IMAGE_MODEL

    t0 = time.monotonic()
    try:
        if provider == "openai":
            model_used = _generate_thumbnail_openai(
                deliverable_id=COMPARISON_DELIVERABLE_ID,
                image_prompt=COMPARISON_PROMPT,
                aspect_ratio=COMPARISON_ASPECT_RATIO,
                config=cfg,
                local_path=local_path,
                log=log,
            )
            _normalize_thumbnail(local_path, COMPARISON_ASPECT_RATIO)
            _apply_wordmark_overlay(local_path)

        elif provider == "openrouter":
            model_used = _generate_thumbnail_openrouter(
                deliverable_id=COMPARISON_DELIVERABLE_ID,
                image_prompt=COMPARISON_PROMPT,
                aspect_ratio=COMPARISON_ASPECT_RATIO,
                config=cfg,
                local_path=local_path,
                log=log,
            )
            _normalize_thumbnail(local_path, COMPARISON_ASPECT_RATIO)
            _apply_wordmark_overlay(local_path)

        elif provider == "muapi":
            # _attempt_thumbnail_generation writes to out_dir/thumbnails/comparison.jpg
            muapi_work_dir = out_dir / "muapi_work"
            muapi_work_dir.mkdir(parents=True, exist_ok=True)
            model_used, _ = _attempt_thumbnail_generation(
                deliverable_id=COMPARISON_DELIVERABLE_ID,
                image_prompt=COMPARISON_PROMPT,
                aspect_ratio=COMPARISON_ASPECT_RATIO,
                primary_model=cfg.THUMBNAIL_IMAGE_MODEL,
                fallback_model="",
                config=cfg,
                output_dir=muapi_work_dir,
                episode_id=f"comparison_{run_id[:8]}",
                budget_guard=None,
                text_overlay=None,
                typography_plan=None,
                typography_integration="none",
                log=log,
            )
            # Move to canonical path
            muapi_written = muapi_work_dir / "thumbnails" / f"{COMPARISON_DELIVERABLE_ID}.jpg"
            if muapi_written.exists():
                import shutil
                shutil.move(str(muapi_written), str(local_path))
        else:
            raise ValueError(f"Unknown provider: {provider!r}")

        elapsed_ms = round((time.monotonic() - t0) * 1000)
        result["status"]     = "success"
        result["latency_ms"] = elapsed_ms
        result["model"]      = model_used or result["model"]

    except MUAPIAuthFailure as exc:
        result["status"]     = "auth_failure"
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
        result["error"]      = str(exc)

    except MUAPIQuotaExhausted as exc:
        result["status"]     = "quota_exhausted"
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
        result["error"]      = str(exc)

    except MUAPIError as exc:
        result["status"]     = "error"
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
        result["error"]      = str(exc)

    except Exception as exc:
        result["status"]     = "unexpected_error"
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
        result["error"]      = f"{type(exc).__name__}: {exc}"

    return result


def run_provider_comparison(
    db_path: Optional[Path] = None,
    providers: Optional[list] = None,
) -> ComparisonRun:
    """
    Run a controlled thumbnail generation comparison across image providers.

    Credentials are resolved via Secret Profile → env var fallback per provider.
    Results are persisted to the Factory OS DB (provider_comparison_runs table).

    Args:
        db_path:   SQLite DB path. Defaults to .hypogeum-assets/hypogeum.db.
        providers: List of provider IDs to test. Defaults to all three:
                   ["openai", "openrouter", "muapi"].

    Returns:
        ComparisonRun with run_id, prompt, and per-provider result list.
    """
    from hypogeum.config import Config
    from hypogeum.db import (
        get_connection,
        initialize_schema,
        insert_provider_comparison_run,
        complete_provider_comparison_run,
    )

    db = Path(db_path) if db_path else _DEFAULT_DB_PATH
    initialize_schema(db)

    if providers is None:
        providers = ["openai", "openrouter", "muapi"]

    run_id = str(uuid.uuid4())
    run_at = _now_iso()

    # Open DB and insert the run record
    conn = get_connection(db)
    try:
        insert_provider_comparison_run(conn, run_id, run_at, COMPARISON_PROMPT)
        conn.commit()
    finally:
        conn.close()

    # Load base config (reads env vars)
    try:
        base_config = Config()
    except Exception as exc:
        # Config requires OPENROUTER_API_KEY — if that's missing something is very wrong
        now = _now_iso()
        conn = get_connection(db)
        try:
            conn.execute(
                "UPDATE provider_comparison_runs SET status='failed', completed_at=? WHERE run_id=?",
                (now, run_id),
            )
            conn.commit()
        finally:
            conn.close()
        raise

    # Run each provider
    results = []
    for provider in providers:
        r = _run_one_provider(provider, run_id, base_config, db)
        results.append(r)

    # Persist results
    completed_at = _now_iso()
    conn = get_connection(db)
    try:
        complete_provider_comparison_run(conn, run_id, results, completed_at)
        conn.commit()
    finally:
        conn.close()

    return ComparisonRun(
        run_id=run_id,
        run_at=run_at,
        prompt=COMPARISON_PROMPT,
        results=results,
        status="completed",
        completed_at=completed_at,
    )


def get_comparison_result(db_path: Optional[Path] = None) -> Optional[dict]:
    """
    Return the most recent completed comparison run from DB, or None.
    """
    from hypogeum.db import get_connection, initialize_schema, get_latest_provider_comparison

    db = Path(db_path) if db_path else _DEFAULT_DB_PATH
    try:
        initialize_schema(db)
        conn = get_connection(db)
        try:
            return get_latest_provider_comparison(conn)
        finally:
            conn.close()
    except Exception:
        return None
