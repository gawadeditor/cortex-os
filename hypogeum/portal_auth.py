"""
hypogeum/portal_auth.py — who may open the client portal (multi-tenant plan, Phase 6).

Passwordless: a client enters their email, gets a one-time link (15 minutes), and the link opens a session
cookie (7 days). Nothing secret is stored in clear: login tokens and session ids are kept as SHA-256 hashes.

    invite(email, workspace_id, role='owner', db_path=None)        platform side: allow this email in
    request_login_link(email, db_path=None) -> token | None        None when the email is unknown/disabled
    consume_login_link(token, db_path=None) -> session_token | None   single use, expiry enforced
    resolve_session(session_token, db_path=None) -> dict | None    {user_id, email, workspace_id, role}
    logout(session_token, db_path=None)
    deliver_link(email, link, config) -> bool                      SMTP when configured, else False

Rules that keep tenants apart: a portal user belongs to exactly ONE workspace; every portal route resolves
its data through that workspace (see hypogeum/client_portal.py). `request_login_link` is rate limited per
email, and callers must answer identically whether or not the email exists (no account enumeration).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

LOGIN_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=7)
MAX_LINKS_PER_HOUR = 5
ROLES = ("owner", "viewer")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_DDL = """
CREATE TABLE IF NOT EXISTS portal_users (
    user_id       TEXT NOT NULL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    workspace_id  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('owner','viewer')),
    status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_login_tokens (
    token_hash  TEXT NOT NULL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE TABLE IF NOT EXISTS portal_sessions (
    session_hash  TEXT NOT NULL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _conn(db_path):
    from hypogeum.db import get_connection  # noqa: PLC0415
    c = get_connection(Path(db_path) if db_path else None)
    c.executescript(_DDL)
    return c


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL.match(e) or len(e) > 254:
        raise ValueError("invalid email")
    return e


def invite(email: str, workspace_id: str, role: str = "owner", db_path=None) -> dict:
    email = normalize_email(email)
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM portal_users WHERE email = ?", (email,)).fetchone()
        if row and row["workspace_id"] != workspace_id:
            raise ValueError("this email already belongs to another workspace")   # one user = one workspace
        if row:
            conn.execute("UPDATE portal_users SET role = ?, status = 'active' WHERE email = ?", (role, email))
            uid = row["user_id"]
        else:
            uid = str(uuid.uuid4())
            conn.execute("INSERT INTO portal_users (user_id, email, workspace_id, role, created_at) VALUES (?,?,?,?,?)",
                         (uid, email, workspace_id, role, _now().isoformat()))
        conn.commit()
        return {"user_id": uid, "email": email, "workspace_id": workspace_id, "role": role}
    finally:
        conn.close()


def disable(email: str, db_path=None) -> None:
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE portal_users SET status = 'disabled' WHERE email = ?", (normalize_email(email),))
        conn.execute("DELETE FROM portal_sessions WHERE user_id IN (SELECT user_id FROM portal_users WHERE email = ?)",
                     (normalize_email(email),))
        conn.commit()
    finally:
        conn.close()


def request_login_link(email: str, db_path=None, now: Optional[datetime] = None) -> Optional[str]:
    """A fresh one-time token for an active user, else None (unknown, disabled, bad email, or rate limited)."""
    now = now or _now()
    try:
        email = normalize_email(email)
    except ValueError:
        return None
    conn = _conn(db_path)
    try:
        u = conn.execute("SELECT * FROM portal_users WHERE email = ? AND status = 'active'", (email,)).fetchone()
        if u is None:
            return None
        recent = conn.execute("SELECT COUNT(*) FROM portal_login_tokens WHERE user_id = ? AND created_at > ?",
                              (u["user_id"], (now - timedelta(hours=1)).isoformat())).fetchone()[0]
        if recent >= MAX_LINKS_PER_HOUR:
            return None
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO portal_login_tokens (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                     (_h(token), u["user_id"], now.isoformat(), (now + LOGIN_TTL).isoformat()))
        conn.commit()
        return token
    finally:
        conn.close()


def consume_login_link(token: str, db_path=None, now: Optional[datetime] = None) -> Optional[str]:
    now = now or _now()
    if not token:
        return None
    conn = _conn(db_path)
    try:
        # one atomic statement: only an unused, unexpired token of an ACTIVE user flips to used
        cur = conn.execute(
            "UPDATE portal_login_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ? "
            "AND user_id IN (SELECT user_id FROM portal_users WHERE status = 'active')",
            (now.isoformat(), _h(token), now.isoformat()))
        if cur.rowcount != 1:
            conn.commit()
            return None
        uid = conn.execute("SELECT user_id FROM portal_login_tokens WHERE token_hash = ?", (_h(token),)).fetchone()[0]
        session = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO portal_sessions (session_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                     (_h(session), uid, now.isoformat(), (now + SESSION_TTL).isoformat()))
        conn.commit()
        return session
    finally:
        conn.close()


def resolve_session(session_token: str, db_path=None, now: Optional[datetime] = None) -> Optional[dict]:
    now = now or _now()
    if not session_token:
        return None
    conn = _conn(db_path)
    try:
        r = conn.execute(
            "SELECT u.user_id, u.email, u.workspace_id, u.role FROM portal_sessions s JOIN portal_users u ON u.user_id = s.user_id "
            "WHERE s.session_hash = ? AND s.expires_at > ? AND u.status = 'active'",
            (_h(session_token), now.isoformat())).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def logout(session_token: str, db_path=None) -> None:
    if not session_token:
        return
    conn = _conn(db_path)
    try:
        conn.execute("DELETE FROM portal_sessions WHERE session_hash = ?", (_h(session_token),))
        conn.commit()
    finally:
        conn.close()


def deliver_link(email: str, link: str, config) -> bool:
    """Send the login link by SMTP when PORTAL_SMTP_HOST is configured. False = not sent (the platform
    operator can still hand the link over: `scripts/workspaces.py login-link`)."""
    host = getattr(config, "PORTAL_SMTP_HOST", None)
    if not host:
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = "Your Cortex sign-in link"
        msg["From"] = getattr(config, "PORTAL_SMTP_FROM", None) or "no-reply@cortexchannels.com"
        msg["To"] = email
        msg.set_content(f"Open this link to sign in (valid 15 minutes, single use):\n\n{link}\n\n"
                        "If you did not ask for it, ignore this email.")
        with smtplib.SMTP(host, int(getattr(config, "PORTAL_SMTP_PORT", 587) or 587), timeout=15) as s:
            s.starttls()
            user = getattr(config, "PORTAL_SMTP_USER", None)
            if user:
                s.login(user, getattr(config, "PORTAL_SMTP_PASSWORD", "") or "")
            s.send_message(msg)
        return True
    except Exception:  # noqa: BLE001
        return False
