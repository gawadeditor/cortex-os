"""
hypogeum/grant_signing.py — dashboard-triggered Channel Operating Grant signing.

ADR-016 keeps grant signing off the long-running production processes: the
Ed25519 private key is only ever read by a short-lived subprocess
(``tools/configure_channel_operating_grant.py --apply``) that signs one
grant and exits. This module is the thin bridge the dashboard uses to
spawn that subprocess and to preview what a grant would authorize.

Nothing here imports ``hypogeum.approval_artifact.sign_approval_artifact``.

Public API:
    can_sign_from_dashboard(config=None) -> bool
    preview_channel_operating_grant(channel_id, db_path=None, days_valid=90) -> dict
    sign_channel_operating_grant(channel_id, *, approver_identity,
        days_valid=90, db_path=None, timeout=180) -> dict
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_TOOL = _ROOT / "tools" / "configure_channel_operating_grant.py"


def _key_path(config=None) -> Optional[Path]:
    try:
        if config is None:
            from hypogeum.config import Config
            config = Config()
        raw = getattr(config, "AUTHORIZATION_APPROVAL_PRIVATE_KEY_PATH", "") or ""
    except Exception:  # noqa: BLE001
        raw = "approval_ed25519_private_key.pem"
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (_ROOT / p)
    return p


def can_sign_from_dashboard(config=None) -> bool:
    """True when the private key file is present + readable and a public
    key is configured for the runtime to verify against."""
    try:
        if config is None:
            from hypogeum.config import Config
            config = Config()
        if not getattr(config, "AUTHORIZATION_APPROVAL_PUBLIC_KEY", None):
            return False
    except Exception:  # noqa: BLE001
        return False
    kp = _key_path(config)
    return bool(kp and kp.is_file() and os.access(kp, os.R_OK) and _TOOL.is_file())


def _db_arg(db_path: Optional[Path]) -> str:
    if db_path is not None:
        return str(Path(db_path).resolve())
    from hypogeum.db import _DEFAULT_DB_PATH
    return str(_DEFAULT_DB_PATH)


def preview_channel_operating_grant(channel_id: str,
                                    db_path: Optional[Path] = None,
                                    days_valid: int = 90) -> dict:
    """What signing would authorize — no key, no signing, no writes."""
    try:
        sys.path.insert(0, str(_ROOT))
        from tools.configure_channel_operating_grant import build_spec  # noqa: PLC0415
        spec = build_spec(channel_id, Path(_db_arg(db_path)), days_valid=days_valid)
        return {
            "ok": True,
            "weekly_ceiling_usd": spec["weekly_exposure_ceiling_usd"],
            "currency": spec["currency"],
            "valid_days": days_valid,
            "valid_from": spec["valid_from"],
            "expires_at": spec["expires_at"],
            "domains": spec["allowed_provider_domains"],
            "binding_count": len(spec["bindings"]),
            "workflow": f"{spec['workflow_id']}@{spec['workflow_version']}",
            "channel_package_version": spec["channel_package_version"],
        }
    except SystemExit as exc:  # build_spec raises SystemExit on missing identity
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


_GRANT_RE = re.compile(r"Channel Operating Grant registered:\s*(\S+?)@")


def sign_channel_operating_grant(channel_id: str, *, approver_identity: str,
                                 days_valid: int = 90,
                                 db_path: Optional[Path] = None,
                                 timeout: int = 180) -> dict:
    """
    Spawn tools/configure_channel_operating_grant.py --apply. The subprocess
    reads the key, signs one grant, registers it (re-verified against the
    public key), and exits. Returns {ok, grant_id?, returncode, stdout_tail,
    stderr_tail, error?}. Never raises.
    """
    if not can_sign_from_dashboard():
        return {"ok": False, "error": "dashboard signing unavailable — the "
                "Ed25519 private key is not on this machine",
                "returncode": None, "stdout_tail": "", "stderr_tail": ""}

    kp = _key_path()
    db = _db_arg(db_path)
    cmd = [
        sys.executable, str(_TOOL),
        "--channel", channel_id,
        "--apply",
        "--db", db,
        "--i-understand-this-is-production",
        "--private-key", str(kp),
        "--approver-identity", approver_identity[:120],
        "--days-valid", str(int(days_valid)),
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(_ROOT), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"signing timed out after {timeout}s",
                "returncode": None, "stdout_tail": "", "stderr_tail": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not spawn the signer: {exc}",
                "returncode": None, "stdout_tail": "", "stderr_tail": ""}

    out, err = proc.stdout or "", proc.stderr or ""
    m = _GRANT_RE.search(out)
    ok = proc.returncode == 0 and m is not None
    return {
        "ok": ok,
        "grant_id": m.group(1) if m else None,
        "returncode": proc.returncode,
        "stdout_tail": out[-1500:],
        "stderr_tail": err[-1500:],
        "error": None if ok else (err.strip().splitlines()[-1] if err.strip()
                                  else f"signer exited {proc.returncode}"),
    }
