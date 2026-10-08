"""
hypogeum/publishing/cover_guard_sync.py — keep the server-side cover release
guard (scripts/server/cover_release_guard.py) in step with this machine.

The YouTube cover hold (youtube_cover_hold.py) still runs here: it holds new
scheduled Shorts and the operator presses Done in the Content Grid. What used
to need this machine awake — flipping a confirmed video public at its slot —
is also done by the always-on server, so a sleeping laptop no longer leaves a
video private past its time. Two calls, both over SSH, both never raise:

  push_state(db_path)     holds + confirmations -> server ~/cover-guard/state.json
  pull_released(db_path)  server ~/cover-guard/released.json -> mark local holds released

Disabled (no-op) unless Config.COVER_GUARD_SSH_TARGET is set.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Callable, Optional

from hypogeum.config import Config
from hypogeum.db import get_connection, initialize_schema
from hypogeum.logger import get_logger

log = get_logger(agent="cover_guard_sync")

_REMOTE_DIR = "cover-guard"
_TIMEOUT_S = 25
_RELEASABLE = ("held", "late", "release_failed")

Runner = Callable[..., subprocess.CompletedProcess]


def _ssh_base() -> Optional[list]:
    cfg = Config()
    target = (cfg.COVER_GUARD_SSH_TARGET or "").strip()
    if not target:
        return None
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if (cfg.COVER_GUARD_SSH_KEY or "").strip():
        cmd += ["-i", cfg.COVER_GUARD_SSH_KEY.strip()]
    return cmd + [target]


def build_state(db_path=None) -> dict:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        postiz_ids = {r[0] for r in conn.execute(
            "SELECT r.provider_publication_id FROM publishing_receipts r "
            "JOIN publishing_attempts a ON a.attempt_id = r.attempt_id WHERE a.provider_id = 'postiz'")}
        # A Postiz hold is released by the server only once youtube.json (the channel's
        # OAuth token) is there; the laptop keeps releasing them itself until then.
        holds = [{"post_id": r["post_id"], "channel_id": r["channel_id"], "release_at": r["release_at"],
                  "upload_at": r["upload_at"], "status": r["status"],
                  "provider": "postiz" if r["post_id"] in postiz_ids else "zernio"}
                 for r in conn.execute(
                     "SELECT post_id, channel_id, release_at, upload_at, status FROM youtube_cover_holds")]
        confirmed = sorted(r["post_id"] for r in conn.execute("SELECT post_id FROM youtube_cover_confirmations"))
    finally:
        conn.close()
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "holds": holds, "confirmed": confirmed}


def push_state(db_path=None, *, runner: Runner = subprocess.run) -> bool:
    base = _ssh_base()
    if base is None:
        return False
    try:
        payload = json.dumps(build_state(db_path))
        remote = (f"mkdir -p ~/{_REMOTE_DIR} && chmod 700 ~/{_REMOTE_DIR} && "
                  f"cat > ~/{_REMOTE_DIR}/state.json.tmp && chmod 600 ~/{_REMOTE_DIR}/state.json.tmp && "
                  f"mv ~/{_REMOTE_DIR}/state.json.tmp ~/{_REMOTE_DIR}/state.json")
        r = runner(base + [remote], input=payload, capture_output=True, text=True, timeout=_TIMEOUT_S)
        if r.returncode != 0:
            log.warn("cover_guard_push_failed", rc=r.returncode, stderr=(r.stderr or "")[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warn("cover_guard_push_error", error=str(exc)[:200])
        return False


def pull_released(db_path=None, *, runner: Runner = subprocess.run) -> list:
    """Mark local holds the server already released. Returns those post_ids."""
    base = _ssh_base()
    if base is None:
        return []
    try:
        r = runner(base + [f"cat ~/{_REMOTE_DIR}/released.json 2>/dev/null || echo '{{}}'"],
                   capture_output=True, text=True, timeout=_TIMEOUT_S)
        if r.returncode != 0:
            log.warn("cover_guard_pull_failed", rc=r.returncode, stderr=(r.stderr or "")[:200])
            return []
        released = json.loads(r.stdout or "{}")
        if not released:
            return []
        conn = get_connection(db_path)
        marked = []
        try:
            for pid, info in released.items():
                cur = conn.execute(
                    f"UPDATE youtube_cover_holds SET status='released', last_error=NULL, "
                    f"released_at=COALESCE(released_at, ?) WHERE post_id=? AND status IN "
                    f"({','.join('?' * len(_RELEASABLE))})",
                    (info.get("released_at"), pid, *_RELEASABLE))
                if cur.rowcount:
                    marked.append(pid)
            conn.commit()
        finally:
            conn.close()
        for pid in marked:
            log.info("cover_guard_release_synced", post_id=pid)
        return marked
    except Exception as exc:  # noqa: BLE001
        log.warn("cover_guard_pull_error", error=str(exc)[:200])
        return []
