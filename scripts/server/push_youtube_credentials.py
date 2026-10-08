#!/usr/bin/env python3
"""
push_youtube_credentials.py — run on the laptop, by the operator.

Copies to the always-on server (the COVER_GUARD_SSH_TARGET host) what its
cover_release_guard needs to flip Postiz-published YouTube Shorts public when
the laptop is asleep:

  ~/cover-guard/youtube.json  OAuth client + one refresh token per channel
  ~/cover-guard/postiz.key    the Postiz public-API key
  ~/cover-guard/cover_release_guard.py   the current guard script

Files are written chmod 600 over SSH; nothing is printed or logged. Without
--apply it only reports what it would send (never the values).

    python3 scripts/server/push_youtube_credentials.py            # dry run
    python3 scripts/server/push_youtube_credentials.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hypogeum.config import Config  # noqa: E402
from hypogeum.credential_manager import resolve_oauth_credential  # noqa: E402
from hypogeum.oauth_service import youtube_oauth_label  # noqa: E402
from hypogeum.publishing.cover_guard_sync import _REMOTE_DIR, _ssh_base  # noqa: E402
from hypogeum.workspace_keys import publishing_key  # noqa: E402


def _channels() -> list:
    from hypogeum.db import get_connection
    conn = get_connection(None)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM channel_provider_bindings "
            "WHERE provider_id = 'postiz' AND platform = 'youtube' AND enabled = 1")]
    finally:
        conn.close()


def _send(base: list, remote_name: str, payload: str, mode: str = "600") -> None:
    tmp = f"~/{_REMOTE_DIR}/{remote_name}.tmp"
    cmd = (f"mkdir -p ~/{_REMOTE_DIR} && chmod 700 ~/{_REMOTE_DIR} && cat > {tmp} && "
           f"chmod {mode} {tmp} && mv {tmp} ~/{_REMOTE_DIR}/{remote_name}")
    r = subprocess.run(base + [cmd], input=payload, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"upload of {remote_name} failed: {(r.stderr or '')[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    base = _ssh_base()
    if base is None:
        raise SystemExit("COVER_GUARD_SSH_TARGET is not configured")
    cfg = Config()
    if not (cfg.YOUTUBE_CLIENT_ID and cfg.YOUTUBE_CLIENT_SECRET):
        raise SystemExit("YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET are not configured")
    channels, missing = {}, []
    for ch in _channels():
        token = resolve_oauth_credential(providers=["youtube"], label=youtube_oauth_label(ch))
        (channels.__setitem__(ch, token) if token else missing.append(ch))
    if missing:
        raise SystemExit(f"no YouTube authorization yet for: {', '.join(missing)} — "
                         f"open /platforms/youtube/authorize?channel=<id> first")
    if not channels:
        raise SystemExit("no channel publishes YouTube through Postiz")
    pz_key = publishing_key("postiz")
    if not pz_key:
        raise SystemExit("no active Postiz API key in Credential Manager")

    print(f"would send youtube.json for {sorted(channels)}, postiz.key, and the guard script"
          if not args.apply else f"sending to {base[-1]} …")
    if not args.apply:
        return 0
    _send(base, "youtube.json", json.dumps({
        "client_id": cfg.YOUTUBE_CLIENT_ID, "client_secret": cfg.YOUTUBE_CLIENT_SECRET,
        "channels": channels}))
    _send(base, "postiz.key", pz_key + "\n")
    _send(base, "cover_release_guard.py", (ROOT / "scripts/server/cover_release_guard.py").read_text(), "644")
    print("done: youtube.json, postiz.key, cover_release_guard.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
