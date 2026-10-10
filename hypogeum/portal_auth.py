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
INVITE_TTL = timedelta(days=7)
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
    created_at    TEXT NOT NULL,
    notify_email  INTEGER NOT NULL DEFAULT 1
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
    if "notify_email" not in {r[1] for r in c.execute("PRAGMA table_info(portal_users)")}:   # tables from Phase 6
        c.execute("ALTER TABLE portal_users ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 1")
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


def request_login_link(email: str, db_path=None, now: Optional[datetime] = None,
                       ttl: Optional[timedelta] = None) -> Optional[str]:
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
                     (_h(token), u["user_id"], now.isoformat(), (now + (ttl or LOGIN_TTL)).isoformat()))
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


def send_email(to: str, subject: str, text: str, config) -> bool:
    """Plain-text email over SMTP (PORTAL_SMTP_*). False = not configured or failed; never raises."""
    host = getattr(config, "PORTAL_SMTP_HOST", None)
    if not host:
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = getattr(config, "PORTAL_SMTP_FROM", None) or "no-reply@cortexchannels.com"
        msg["To"] = to
        msg.set_content(text)
        with smtplib.SMTP(host, int(getattr(config, "PORTAL_SMTP_PORT", 587) or 587), timeout=15) as s:
            s.starttls()
            user = getattr(config, "PORTAL_SMTP_USER", None)
            if user:
                s.login(user, getattr(config, "PORTAL_SMTP_PASSWORD", "") or "")
            s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        from hypogeum.logger import get_logger  # noqa: PLC0415
        get_logger(agent="portal_auth").warn("portal_email_failed", error=type(exc).__name__, detail=str(exc)[:120])
        return False


def deliver_link(email: str, link: str, config) -> bool:
    """The sign-in link email. False = not sent (the operator can still hand the link over:
    `scripts/workspaces.py login-link`)."""
    return send_email(email, "Your Cortex sign-in link",
                      f"Open this link to sign in (valid 15 minutes, single use):\n\n{link}\n\n"
                      "If you did not ask for it, ignore this email.", config)


def _base_url(config) -> str:
    return (getattr(config, "PORTAL_BASE_URL", None) or "https://app.cortexchannels.com").rstrip("/")


def invite_and_send(email: str, workspace_id: str, role: str = "owner", db_path=None, config=None,
                    workspace_name: Optional[str] = None) -> dict:
    """Allow `email` into the workspace's portal and email a welcome with a sign-in link that stays valid for
    INVITE_TTL (a first-time invitee may not open it at once). Returns {user, emailed, link} -- `link` is
    returned so the operator can hand it over when email is not configured or failed."""
    user = invite(email, workspace_id, role, db_path)
    token = request_login_link(user["email"], db_path, ttl=INVITE_TTL)
    link = f"{_base_url(config)}/auth/{token}" if token else None
    emailed = False
    if link:
        name = workspace_name or workspace_id
        emailed = send_email(
            user["email"], f"You have been invited to Cortex ({name})",
            f"You now have access to the Cortex portal for {name}: your channels, what we have published for you, "
            f"this month's spending and anything that needs your attention.\n\nSign in with this link "
            f"(valid 7 days, single use):\n\n{link}\n\nAfter that, you can request a new sign-in link any time from "
            f"{_base_url(config)}/login using this email address.", config)
    return {"user": user, "emailed": emailed, "link": link}


def workspace_emails(workspace_id: str, db_path=None) -> list:
    conn = _conn(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT email FROM portal_users WHERE workspace_id = ? AND status = 'active' AND notify_email = 1",
            (workspace_id,))]
    finally:
        conn.close()


def set_notify(user_id: str, on: bool, db_path=None) -> None:
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE portal_users SET notify_email = ? WHERE user_id = ?", (1 if on else 0, user_id))
        conn.commit()
    finally:
        conn.close()


def send_workspace_alert_email(workspace_id: str, text: str, db_path=None, config=None) -> bool:
    """Email an alert (the same text the workspace's Telegram chats get) to the workspace's portal users who
    have not turned email off. Only CLIENT workspaces are emailed -- the operator has Telegram. True if any sent."""
    try:
        from hypogeum import workspaces as ws  # noqa: PLC0415
        kind = {w["workspace_id"]: w["kind"] for w in ws.list_workspaces(Path(db_path) if db_path else None)}.get(workspace_id)
        if kind != "client":
            return False
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        subject = (lines[0] if lines else "Cortex notification").lstrip("🔴✅🛠 ")[:120]
        body = text.strip() + f"\n\n—\nOpen your portal: {_base_url(config)}\nTo stop these emails, turn off 'Email me about problems' there."
        return any([send_email(e, subject, body, config) for e in workspace_emails(workspace_id, db_path)])
    except Exception:  # noqa: BLE001
        return False
