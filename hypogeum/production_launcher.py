"""
hypogeum/production_launcher.py — Dashboard → Pipeline launcher (F14)

Public function:
    launch_episode_from_dashboard(...) -> dict
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
) -> dict:
    """
    Preflight all required resources, then spawn run_pipeline.py via Popen.

    Returns:
        {"status": "launched", "episode_id": str, "pid": int}   — success
        {"status": "blocked",  "missing": list[str]}             — preflight failed
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

    # --- create episode record before spawning ---
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

    # --- spawn pipeline (non-blocking) ---
    run_pipeline_path = _PROJECT_ROOT / "run_pipeline.py"
    cmd = [
        sys.executable,
        str(run_pipeline_path),
        "--topic", topic,
        "--episode-id", episode_id,
        "--channel-id", channel_id,
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        start_new_session=True,
    )

    return {"status": "launched", "episode_id": episode_id, "pid": proc.pid}
