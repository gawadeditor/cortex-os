"""
hypogeum/production_launcher.py — Dashboard → Queue submission (F14)

Public function:
    launch_episode_from_dashboard(...) -> dict

Validates a manually-submitted episode request, then places it on the same
canonical queue the Scheduler uses instead of executing it directly:

    Dashboard -> hypogeum.dashboard_control.enqueue_topic()
              -> hypogeum.queue.enqueue()
              -> run_worker (existing poll loop, unmodified)
              -> queue.process_next()
              -> worker_integration (gated executor, unmodified)
              -> pipeline.run_pipeline()

The submitted item is picked up by run_worker.py's existing poll loop and
gated by hypogeum.worker_integration's executor exactly like an autonomous
item. This module contains no execution-orchestration logic — no
subprocess, no direct pipeline.run_pipeline() call, no parallel execution
path. episode_id is generated and recorded immediately (as before this
change), then threaded through to enqueue_topic()/queue.enqueue() so the
worker's eventual claim resolves to this exact episode_id — the same
"preserve episode_id" pattern already used for reruns (see
run_dashboard.py's /api/run/<id>/rerun).
"""

from __future__ import annotations

import os
import shutil
import uuid
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


def _generate_episode_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"ep_{ts}_{short}"


def launch_episode_from_dashboard(
    channel_id: str,
    topic: str,
    video_type: str,
    quality_mode: str,
    platforms: list,
    db_path=None,
    youtube_privacy: str = "private",
) -> dict:
    """
    Preflight all required resources, then submit the episode to the
    canonical queue for the worker to pick up.

    video_type/quality_mode/youtube_privacy/platforms are accepted for
    signature compatibility; platforms was already unused in the prior
    direct-execution implementation (platform selection is governed
    downstream by ChannelOperationsProfile.enabled_platforms, not
    per-submission choice) and remains unused here.

    Returns:
        {"status": "launched", "episode_id": str, "queue_id": str, "pid": None} — success
        {"status": "blocked",  "missing": list[str]}                           — preflight failed

    "pid" is kept (always None) for response-shape compatibility with
    callers that previously read it — there is no longer a spawned process.
    """
    missing: list[str] = []

    # --- env var checks ---
    for var in _REQUIRED_ENV_VARS:
        if not os.environ.get(var, "").strip():
            missing.append(f"env:{var}")

    # --- ffmpeg check ---
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg not found on PATH")

    # --- channel checks ---
    channel = None
    if not channel_id:
        missing.append("channel_id is required")
    else:
        try:
            from hypogeum.channel_config import get_channel_profile as _get_ch
            db = Path(db_path) if db_path else None
            channel = _get_ch(channel_id, db_path=db)
        except Exception:
            channel = None

        if channel is None:
            missing.append(f"channel:{channel_id} not found")
        elif getattr(channel, "status", None) != "active":
            status_val = getattr(channel, "status", "unknown")
            missing.append(f"channel:{channel_id} is not active (status={status_val})")

    if missing:
        return {"status": "blocked", "missing": missing}

    # --- create episode record before submitting ---
    episode_id = _generate_episode_id()
    db_resolved = Path(db_path) if db_path else _DEFAULT_DB_PATH

    try:
        from hypogeum.db import get_connection, initialize_schema, insert_episode
        initialize_schema(db_resolved)
        conn = get_connection(db_resolved)
        try:
            now = datetime.now(timezone.utc).isoformat()
            insert_episode(conn, episode_id=episode_id, created_at=now,
                           topic=topic, status="in_progress")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return {"status": "blocked", "missing": [f"DB error creating episode: {exc}"]}

    # --- submit to the canonical queue — never executes directly ---
    from hypogeum.dashboard_control import enqueue_topic
    enqueue_result = enqueue_topic(
        channel_id, topic,
        priority=0,
        db_path=db_path,
        source="manual",
        episode_id=episode_id,
    )
    if not enqueue_result.get("ok"):
        return {"status": "blocked", "missing": [enqueue_result.get("error", "enqueue failed")]}

    from hypogeum.queue import enqueue as enqueue_queue_item
    queue_id = enqueue_queue_item(
        db_path=db_path,
        channel_id=channel_id,
        episode_id=episode_id,
        priority=0,
        source="manual",
    )

    return {"status": "launched", "episode_id": episode_id, "queue_id": queue_id, "pid": None}
