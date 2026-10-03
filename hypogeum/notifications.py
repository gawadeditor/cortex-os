"""
hypogeum/notifications.py — Notification Delivery

Routes OIS signals to configured delivery channels (Telegram, email).
Persists every delivery attempt to `notification_deliveries` table.

Public API:
    route_signal(signal, config, db_path=None) -> list[str]
      # Delivers to all configured channels. Returns list of delivery_ids.
      # Respects NOTIFY_SEVERITY_THRESHOLD; skips INFO by default.
      # Deduplicates: will not re-send for a signal that already has a
      # 'sent' delivery record.

    send_telegram(message, bot_token, chat_id) -> bool
    send_email(subject, body, smtp_config) -> bool

    format_signal_message(signal) -> str
      # Human-readable message including severity, type, timestamp, message.

    get_delivery_history(limit=50, db_path=None) -> list[DeliveryRecord]

Delivery channels:
    telegram — POST to Telegram Bot API (requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
    email    — SMTP with STARTTLS (requires NOTIFY_EMAIL_FROM/TO + NOTIFY_SMTP_HOST)

Cooldown/dedup:
    1. OIS-level dedup: emit_signal() deduplicates signals within a cooldown
       window, so a repeated signal type produces no new DB row → no notification.
    2. Notification-level dedup: route_signal() checks if a 'sent' delivery
       already exists for this signal_id and skips re-delivery if found.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection
from hypogeum.ois import SignalRecord


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_SEVERITY_ORDER: dict[str, int] = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass
class DeliveryRecord:
    delivery_id:  str
    signal_id:    Optional[str]
    channel_name: str
    recipient:    str
    subject:      Optional[str]
    message:      str
    status:       str           # "pending" | "sent" | "failed"
    sent_at:      Optional[str]
    error:        Optional[str]
    created_at:   str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _meets_threshold(severity: str, threshold: str) -> bool:
    return _SEVERITY_ORDER.get(severity, 0) >= _SEVERITY_ORDER.get(threshold, 1)


def _already_notified(signal_id: str, db_path: Optional[Path]) -> bool:
    """
    Return True if a sent or pending delivery record exists for this signal_id.

    Covers both success dedup (already sent) and in-flight dedup (pending).
    Failed deliveries are intentionally excluded so they can be retried.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notification_deliveries "
                "WHERE signal_id = ? AND status IN ('sent', 'pending')",
                (signal_id,),
            ).fetchone()
        finally:
            conn.close()
        return (row["cnt"] if row else 0) > 0
    except Exception:
        return False


def _record_delivery(
    channel_name: str,
    recipient: str,
    message: str,
    subject: Optional[str],
    signal_id: Optional[str],
    db_path: Optional[Path],
) -> str:
    """Insert a 'pending' delivery record. Returns delivery_id."""
    delivery_id = str(uuid.uuid4())
    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                """
                INSERT INTO notification_deliveries
                    (delivery_id, signal_id, channel_name, recipient, subject,
                     message, status, sent_at, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)
                """,
                (delivery_id, signal_id, channel_name, recipient,
                 subject, message, now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return delivery_id


def _mark_sent(delivery_id: str, db_path: Optional[Path]) -> None:
    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                "UPDATE notification_deliveries "
                "SET status = 'sent', sent_at = ? WHERE delivery_id = ?",
                (now, delivery_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _mark_failed(delivery_id: str, error: str, db_path: Optional[Path]) -> None:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                "UPDATE notification_deliveries "
                "SET status = 'failed', error = ? WHERE delivery_id = ?",
                (error[:1024], delivery_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_signal_message(signal: SignalRecord) -> str:
    """
    Produce a human-readable notification message from a SignalRecord.

    Output always includes severity, signal_type, message, and timestamp.
    Blocking status and channel_id are appended when present.
    """
    tier_prefix = {
        "CRITICAL": "[CRITICAL]",
        "WARNING":  "[WARNING]",
        "INFO":     "[INFO]",
    }.get(signal.severity, "[SIGNAL]")

    lines = [
        f"{tier_prefix} {signal.signal_type}",
        f"Time:    {signal.created_at}",
        f"Message: {signal.message}",
        f"Source:  {signal.source_system}",
    ]
    if signal.channel_id:
        lines.append(f"Channel: {signal.channel_id}")
    if signal.blocking:
        lines.append("BLOCKING: new runs prevented until this signal is resolved.")
    lines.append(f"ID:      {signal.signal_id}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    """
    Send a message via the Telegram Bot API.

    Returns True on HTTP 200, False on any error (network, auth, etc.).
    Never raises.
    """
    try:
        import requests
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str, smtp_config: dict) -> bool:
    """
    Send an email via SMTP with STARTTLS.

    smtp_config keys: host, port (default 587), from, to, password (optional).
    Returns True on success, False on any error.
    Never raises.
    """
    try:
        import smtplib
        from email.mime.text import MIMEText

        host = smtp_config["host"]
        port = int(smtp_config.get("port", 587))
        from_addr = smtp_config["from"]
        to_addr = smtp_config["to"]
        password = smtp_config.get("password") or ""

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr

        server = smtplib.SMTP(host, port)
        try:
            server.starttls()
            if password:
                server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        finally:
            server.quit()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Signal routing
# ---------------------------------------------------------------------------

def route_signal(
    signal: SignalRecord,
    config,
    db_path: Optional[Path] = None,
) -> list:
    """
    Deliver a signal to all configured notification channels.

    Skips delivery when:
    - signal.severity is below NOTIFY_SEVERITY_THRESHOLD (default WARNING)
    - a 'sent' delivery already exists for this signal_id (deduplication)
    - no channels are configured

    Delivery failures are recorded in notification_deliveries without raising.

    Returns list of delivery_ids (empty if nothing was attempted).
    """
    threshold = getattr(config, "NOTIFY_SEVERITY_THRESHOLD", "WARNING") or "WARNING"
    if not _meets_threshold(signal.severity, threshold):
        return []

    if _already_notified(signal.signal_id, db_path):
        return []

    delivery_ids: list = []

    # ---- Telegram ----
    tg_token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    tg_chat = getattr(config, "TELEGRAM_CHAT_ID", None)
    # Config sets TELEGRAM_RAW_SIGNALS=False by default (operator_alerts owns
    # the operator's Telegram); bare config objects keep the old behaviour.
    if tg_token and tg_chat and getattr(config, "TELEGRAM_RAW_SIGNALS", True):
        body = format_signal_message(signal)
        did = _record_delivery(
            channel_name="telegram",
            recipient=str(tg_chat),
            message=body,
            subject=None,
            signal_id=signal.signal_id,
            db_path=db_path,
        )
        try:
            ok = send_telegram(body, tg_token, tg_chat)
            if ok:
                _mark_sent(did, db_path)
            else:
                _mark_failed(did, "HTTP request failed or non-200 response", db_path)
        except Exception as exc:
            _mark_failed(did, str(exc), db_path)
        delivery_ids.append(did)

    # ---- Email ----
    email_from = getattr(config, "NOTIFY_EMAIL_FROM", None)
    email_to = getattr(config, "NOTIFY_EMAIL_TO", None)
    smtp_host = getattr(config, "NOTIFY_SMTP_HOST", None)
    if email_from and email_to and smtp_host:
        body = format_signal_message(signal)
        subject = f"[{signal.severity}] {signal.signal_type}"
        smtp_config = {
            "host": smtp_host,
            "port": getattr(config, "NOTIFY_SMTP_PORT", 587),
            "from": email_from,
            "to": email_to,
            "password": getattr(config, "NOTIFY_SMTP_PASSWORD", "") or "",
        }
        did = _record_delivery(
            channel_name="email",
            recipient=email_to,
            message=body,
            subject=subject,
            signal_id=signal.signal_id,
            db_path=db_path,
        )
        try:
            ok = send_email(subject, body, smtp_config)
            if ok:
                _mark_sent(did, db_path)
            else:
                _mark_failed(did, "SMTP send failed", db_path)
        except Exception as exc:
            _mark_failed(did, str(exc), db_path)
        delivery_ids.append(did)

    return delivery_ids


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def get_delivery_history(
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> list:
    """
    Return the most recent delivery records ordered by created_at DESC.
    Returns [] on any error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                "SELECT * FROM notification_deliveries "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [
            DeliveryRecord(
                delivery_id=r["delivery_id"],
                signal_id=r["signal_id"],
                channel_name=r["channel_name"],
                recipient=r["recipient"],
                subject=r["subject"],
                message=r["message"],
                status=r["status"],
                sent_at=r["sent_at"],
                error=r["error"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    except Exception:
        return []
