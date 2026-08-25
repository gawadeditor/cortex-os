"""
tests/test_notifications.py — P2-02: Notification Delivery tests

Coverage:
    TestFormatSignalMessage    — includes severity, type, message, timestamp
    TestSendTelegram           — mocked HTTP; correct API call; error handling
    TestSendEmail              — mocked smtplib; correct call; error handling
    TestRouteSignal            — threshold, no-config skip, DB persistence,
                                 failure recording, dedup, both channels
    TestGetDeliveryHistory     — empty; limit; ordering
    TestOISIntegration         — emit_signal routes when config provided; skips when None
    TestNoNetworkCalls         — read-only operations open no sockets
    TestNoProductionSideEffects— no episode/run records written
"""

from __future__ import annotations

import smtplib
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.notifications import (
    DeliveryRecord,
    format_signal_message,
    get_delivery_history,
    route_signal,
    send_email,
    send_telegram,
)
from hypogeum.ois import SignalRecord, emit_signal


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "notif_test.db"
    initialize_schema(p)
    return p


@dataclass
class _FakeConfig:
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    NOTIFY_EMAIL_FROM: Optional[str] = None
    NOTIFY_EMAIL_TO: Optional[str] = None
    NOTIFY_SMTP_HOST: Optional[str] = None
    NOTIFY_SMTP_PORT: int = 587
    NOTIFY_SMTP_PASSWORD: Optional[str] = None
    NOTIFY_SEVERITY_THRESHOLD: str = "WARNING"


def _tg_config(**kw) -> _FakeConfig:
    base = dict(TELEGRAM_BOT_TOKEN="bot123:ABC", TELEGRAM_CHAT_ID="12345678")
    return _FakeConfig(**{**base, **kw})


def _email_config(**kw) -> _FakeConfig:
    base = dict(
        NOTIFY_EMAIL_FROM="from@example.com",
        NOTIFY_EMAIL_TO="to@example.com",
        NOTIFY_SMTP_HOST="smtp.example.com",
        NOTIFY_SMTP_PORT=587,
        NOTIFY_SMTP_PASSWORD="secret",
    )
    return _FakeConfig(**{**base, **kw})


def _make_signal(
    severity="WARNING",
    signal_type="run_failed",
    message="Something went wrong",
    channel_id=None,
    blocking=False,
    signal_id=None,
):
    return SignalRecord(
        signal_id=signal_id or "sig-test-001",
        signal_type=signal_type,
        severity=severity,
        message=message,
        channel_id=channel_id,
        source_system="test",
        blocking=blocking,
        lifecycle_status="open",
        created_at="2026-06-15T12:00:00+00:00",
        resolved_at=None,
        resolved_by=None,
        metadata=None,
        episode_id=None,
        run_id=None,
    )


# ===========================================================================
# TestFormatSignalMessage
# ===========================================================================

class TestFormatSignalMessage:

    def test_includes_severity(self):
        sig = _make_signal(severity="CRITICAL")
        msg = format_signal_message(sig)
        assert "CRITICAL" in msg

    def test_includes_signal_type(self):
        sig = _make_signal(signal_type="budget_exhaustion")
        msg = format_signal_message(sig)
        assert "budget_exhaustion" in msg

    def test_includes_message(self):
        sig = _make_signal(message="Budget ceiling exceeded at $5.00")
        msg = format_signal_message(sig)
        assert "Budget ceiling exceeded at $5.00" in msg

    def test_includes_timestamp(self):
        sig = _make_signal()
        msg = format_signal_message(sig)
        assert "2026-06-15" in msg

    def test_includes_signal_id(self):
        sig = _make_signal(signal_id="sig-unique-xyz")
        msg = format_signal_message(sig)
        assert "sig-unique-xyz" in msg

    def test_includes_source_system(self):
        sig = _make_signal()
        msg = format_signal_message(sig)
        assert "test" in msg

    def test_blocking_flag_mentioned(self):
        sig = _make_signal(blocking=True)
        msg = format_signal_message(sig)
        assert "BLOCKING" in msg.upper()

    def test_non_blocking_no_blocking_text(self):
        sig = _make_signal(blocking=False)
        msg = format_signal_message(sig)
        assert "BLOCKING" not in msg.upper()

    def test_channel_id_included_when_present(self):
        sig = _make_signal(channel_id="hypogeum")
        msg = format_signal_message(sig)
        assert "hypogeum" in msg

    def test_channel_id_absent_when_none(self):
        sig = _make_signal(channel_id=None)
        msg = format_signal_message(sig)
        assert "Channel:" not in msg

    def test_returns_multiline_string(self):
        sig = _make_signal(severity="CRITICAL", signal_type="emergency_halt")
        msg = format_signal_message(sig)
        assert "\n" in msg


# ===========================================================================
# TestSendTelegram
# ===========================================================================

class TestSendTelegram:

    def _mock_200(self):
        resp = MagicMock()
        resp.status_code = 200
        return resp

    def _mock_error(self, status=400):
        resp = MagicMock()
        resp.status_code = status
        return resp

    def test_telegram_called_on_critical_signal(self):
        with patch("requests.post", return_value=self._mock_200()) as mock_post:
            result = send_telegram("Hello!", "bot-token", "chat-123")
        assert result is True
        mock_post.assert_called_once()

    def test_correct_api_url_formed(self):
        with patch("requests.post", return_value=self._mock_200()) as mock_post:
            send_telegram("msg", "mytoken:XYZ", "99999")
        url = mock_post.call_args[0][0]
        assert "mytoken:XYZ" in url
        assert "sendMessage" in url
        assert "api.telegram.org" in url

    def test_correct_payload(self):
        with patch("requests.post", return_value=self._mock_200()) as mock_post:
            send_telegram("test message", "bot-tok", "chat-id-42")
        _, kwargs = mock_post.call_args
        payload = kwargs.get("json") or mock_post.call_args[1].get("json")
        assert payload["chat_id"] == "chat-id-42"
        assert payload["text"] == "test message"

    def test_returns_false_on_http_error(self):
        with patch("requests.post", return_value=self._mock_error(401)):
            result = send_telegram("msg", "bad-token", "chat")
        assert result is False

    def test_returns_false_on_network_exception(self):
        with patch("requests.post", side_effect=ConnectionError("offline")):
            result = send_telegram("msg", "token", "chat")
        assert result is False

    def test_does_not_raise_on_failure(self):
        with patch("requests.post", side_effect=RuntimeError("crash")):
            result = send_telegram("msg", "token", "chat")
        assert result is False  # never raises


# ===========================================================================
# TestSendEmail
# ===========================================================================

class TestSendEmail:

    def _smtp_config(self, **kw):
        base = {
            "host": "smtp.example.com",
            "port": 587,
            "from": "from@example.com",
            "to": "to@example.com",
            "password": "secret",
        }
        return {**base, **kw}

    def test_email_called_on_warning_signal(self):
        mock_smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            result = send_email("Subject", "Body text", self._smtp_config())
        assert result is True
        mock_smtp.sendmail.assert_called_once()

    def test_smtp_starttls_called(self):
        mock_smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email("Subj", "Body", self._smtp_config())
        mock_smtp.starttls.assert_called_once()

    def test_smtp_login_called_with_password(self):
        mock_smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email("Subj", "Body", self._smtp_config(password="mypass"))
        mock_smtp.login.assert_called_once_with("from@example.com", "mypass")

    def test_smtp_quit_called(self):
        mock_smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email("Subj", "Body", self._smtp_config())
        mock_smtp.quit.assert_called_once()

    def test_returns_false_on_smtp_error(self):
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "down")):
            result = send_email("Subj", "Body", self._smtp_config())
        assert result is False

    def test_does_not_raise_on_failure(self):
        with patch("smtplib.SMTP", side_effect=RuntimeError("crash")):
            result = send_email("Subj", "Body", self._smtp_config())
        assert result is False

    def test_no_login_without_password(self):
        mock_smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            send_email("Subj", "Body", self._smtp_config(password=""))
        mock_smtp.login.assert_not_called()


# ===========================================================================
# TestRouteSignal
# ===========================================================================

class TestRouteSignal:

    def test_no_delivery_if_no_config_telegram(self, db_path):
        """Empty config → no delivery, no error."""
        sig = _make_signal(severity="CRITICAL")
        config = _FakeConfig()  # no tokens set
        result = route_signal(sig, config, db_path=db_path)
        assert result == []
        assert get_delivery_history(db_path=db_path) == []

    def test_info_signal_below_threshold_not_delivered(self, db_path):
        """INFO is below default WARNING threshold → no delivery."""
        sig = _make_signal(severity="INFO", signal_type="run_completed")
        config = _tg_config()  # threshold=WARNING by default
        with patch("requests.post") as mock_post:
            result = route_signal(sig, config, db_path=db_path)
        assert result == []
        mock_post.assert_not_called()

    def test_warning_at_threshold_delivered(self, db_path):
        """WARNING == threshold → delivery attempted."""
        sig = _make_signal(severity="WARNING")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            result = route_signal(sig, config, db_path=db_path)
        assert len(result) == 1

    def test_critical_above_threshold_delivered(self, db_path):
        """CRITICAL > WARNING threshold → delivery attempted."""
        sig = _make_signal(severity="CRITICAL", signal_type="budget_exhaustion")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            result = route_signal(sig, config, db_path=db_path)
        assert len(result) == 1

    def test_delivery_recorded_in_db(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-persist-001")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            route_signal(sig, config, db_path=db_path)
        history = get_delivery_history(db_path=db_path)
        assert len(history) == 1
        assert history[0].status == "sent"
        assert history[0].signal_id == "sig-persist-001"
        assert history[0].channel_name == "telegram"

    def test_failed_delivery_recorded_with_error(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-fail-001")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("requests.post", return_value=mock_resp):
            route_signal(sig, config, db_path=db_path)
        history = get_delivery_history(db_path=db_path)
        assert len(history) == 1
        assert history[0].status == "failed"
        assert history[0].error is not None

    def test_exception_during_send_recorded_as_failed(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-exc-001")
        config = _tg_config()
        with patch("requests.post", side_effect=ConnectionError("network down")):
            route_signal(sig, config, db_path=db_path)
        history = get_delivery_history(db_path=db_path)
        assert history[0].status == "failed"

    def test_dedup_no_resend_for_same_signal_id(self, db_path):
        """Calling route_signal twice for the same signal_id sends only once."""
        sig = _make_signal(severity="CRITICAL", signal_id="sig-dedup-001")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mock_post:
            route_signal(sig, config, db_path=db_path)
            route_signal(sig, config, db_path=db_path)  # second call — should skip
        assert mock_post.call_count == 1

    def test_both_channels_used_when_configured(self, db_path):
        """When both Telegram and email are configured, both deliveries are attempted."""
        sig = _make_signal(severity="CRITICAL", signal_id="sig-both-001")
        config = _FakeConfig(
            TELEGRAM_BOT_TOKEN="tok",
            TELEGRAM_CHAT_ID="chat",
            NOTIFY_EMAIL_FROM="from@x.com",
            NOTIFY_EMAIL_TO="to@x.com",
            NOTIFY_SMTP_HOST="smtp.x.com",
            NOTIFY_SMTP_PORT=587,
            NOTIFY_SEVERITY_THRESHOLD="WARNING",
        )
        mock_tg = MagicMock()
        mock_tg.status_code = 200
        mock_smtp = MagicMock()
        with patch("requests.post", return_value=mock_tg):
            with patch("smtplib.SMTP", return_value=mock_smtp):
                result = route_signal(sig, config, db_path=db_path)
        assert len(result) == 2
        history = get_delivery_history(db_path=db_path)
        channels = {r.channel_name for r in history}
        assert "telegram" in channels
        assert "email" in channels

    def test_critical_threshold_skips_warning(self, db_path):
        """When threshold=CRITICAL, WARNING signals are not delivered."""
        sig = _make_signal(severity="WARNING")
        config = _tg_config(NOTIFY_SEVERITY_THRESHOLD="CRITICAL")
        with patch("requests.post") as mock_post:
            result = route_signal(sig, config, db_path=db_path)
        assert result == []
        mock_post.assert_not_called()

    def test_returns_delivery_ids_as_strings(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-ids-001")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            result = route_signal(sig, config, db_path=db_path)
        assert all(isinstance(d, str) for d in result)

    def test_route_does_not_raise_on_db_error(self, tmp_path):
        """route_signal must not raise even when DB is unavailable."""
        bad_db = tmp_path / "bad_dir"
        bad_db.mkdir()
        sig = _make_signal(severity="CRITICAL")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            result = route_signal(sig, config, db_path=bad_db)
        # Returns a delivery_id even without DB (or empty list — either is fine)
        assert isinstance(result, list)


# ===========================================================================
# TestGetDeliveryHistory
# ===========================================================================

class TestGetDeliveryHistory:

    def test_empty_on_no_deliveries(self, db_path):
        assert get_delivery_history(db_path=db_path) == []

    def test_returns_delivery_record_type(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-hist-001")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            route_signal(sig, config, db_path=db_path)
        history = get_delivery_history(db_path=db_path)
        assert isinstance(history[0], DeliveryRecord)

    def test_returns_up_to_limit(self, db_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        config = _tg_config()
        with patch("requests.post", return_value=mock_resp):
            for i in range(5):
                sig = _make_signal(severity="CRITICAL", signal_id=f"sig-lim-{i:03d}")
                route_signal(sig, config, db_path=db_path)
        history = get_delivery_history(limit=3, db_path=db_path)
        assert len(history) == 3

    def test_ordered_by_created_at_desc(self, db_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        config = _tg_config()
        with patch("requests.post", return_value=mock_resp):
            for i in range(3):
                sig = _make_signal(severity="CRITICAL", signal_id=f"sig-ord-{i:03d}")
                route_signal(sig, config, db_path=db_path)
        history = get_delivery_history(db_path=db_path)
        dates = [r.created_at for r in history]
        assert dates == sorted(dates, reverse=True)

    def test_delivery_record_has_correct_fields(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-fields-001",
                           signal_type="budget_exhaustion")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            route_signal(sig, config, db_path=db_path)
        rec = get_delivery_history(db_path=db_path)[0]
        assert rec.signal_id == "sig-fields-001"
        assert rec.channel_name == "telegram"
        assert rec.status == "sent"
        assert rec.sent_at is not None
        assert rec.created_at is not None


# ===========================================================================
# TestOISIntegration
# ===========================================================================

class TestOISIntegration:

    def test_emit_signal_routes_when_config_provided(self, db_path):
        """emit_signal(config=...) triggers notification routing."""
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mock_post:
            emit_signal(
                signal_type="budget_exhaustion",
                severity="CRITICAL",
                message="Budget hit",
                channel_id=None,
                source_system="finance_agent",
                blocking=True,
                config=config,
                db_path=db_path,
            )
        mock_post.assert_called_once()

    def test_emit_signal_skips_routing_without_config(self, db_path):
        """emit_signal() without config does not route notifications."""
        with patch("requests.post") as mock_post:
            emit_signal(
                signal_type="budget_exhaustion",
                severity="CRITICAL",
                message="Budget hit",
                channel_id=None,
                source_system="finance_agent",
                blocking=True,
                db_path=db_path,
                # config not provided
            )
        mock_post.assert_not_called()

    def test_emit_signal_notification_records_in_db(self, db_path):
        """Delivery record is written to notification_deliveries after emit."""
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            emit_signal(
                signal_type="credential_failure",
                severity="CRITICAL",
                message="API down",
                channel_id=None,
                source_system="startup",
                blocking=True,
                config=config,
                db_path=db_path,
            )
        history = get_delivery_history(db_path=db_path)
        assert len(history) == 1
        assert history[0].status == "sent"

    def test_emit_signal_info_below_threshold_no_notify(self, db_path):
        """INFO signal (below WARNING threshold) emits to DB but not to channels."""
        config = _tg_config(NOTIFY_SEVERITY_THRESHOLD="WARNING")
        with patch("requests.post") as mock_post:
            emit_signal(
                signal_type="run_completed",
                severity="INFO",
                message="Done",
                channel_id=None,
                source_system="pipeline",
                blocking=False,
                config=config,
                db_path=db_path,
            )
        mock_post.assert_not_called()

    def test_notification_failure_does_not_affect_signal(self, db_path):
        """Notification failure must not prevent signal from being recorded."""
        config = _tg_config()
        with patch("requests.post", side_effect=RuntimeError("network dead")):
            sid = emit_signal(
                signal_type="budget_exhaustion",
                severity="CRITICAL",
                message="Hit ceiling",
                channel_id=None,
                source_system="finance_agent",
                blocking=True,
                config=config,
                db_path=db_path,
            )
        # Signal must still be in DB
        from hypogeum.ois import get_blocking_signals
        blocking = get_blocking_signals(db_path=db_path)
        assert any(s.signal_id == sid for s in blocking)

    def test_dedup_signal_does_not_trigger_second_notification(self, db_path):
        """Deduplicated signal returns existing ID without re-notifying."""
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mock_post:
            # First emit creates the signal and notifies
            emit_signal(
                signal_type="budget_exhaustion",
                severity="CRITICAL",
                message="Ceiling hit",
                channel_id=None,
                source_system="finance_agent",
                blocking=True,
                config=config,
                db_path=db_path,
            )
            # Second emit within cooldown window — same signal_id returned, no new notify
            emit_signal(
                signal_type="budget_exhaustion",
                severity="CRITICAL",
                message="Ceiling hit again",
                channel_id=None,
                source_system="finance_agent",
                blocking=True,
                config=config,
                db_path=db_path,
            )
        assert mock_post.call_count == 1


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    def test_get_delivery_history_no_sockets(self, db_path, monkeypatch):
        def no_socket(*a, **k):
            raise AssertionError("notifications opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        get_delivery_history(db_path=db_path)

    def test_format_signal_no_sockets(self, monkeypatch):
        def no_socket(*a, **k):
            raise AssertionError("format_signal_message opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        sig = _make_signal()
        format_signal_message(sig)  # pure string formatting — no network


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:

    def test_route_signal_does_not_write_runs(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-safe-001")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            route_signal(sig, config, db_path=db_path)
        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_route_signal_does_not_write_episodes(self, db_path):
        sig = _make_signal(severity="CRITICAL", signal_id="sig-ep-001")
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            route_signal(sig, config, db_path=db_path)
        conn = get_connection(db_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "episodes" in tables:
                n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                assert n == 0
        finally:
            conn.close()


# ===========================================================================
# TestPendingDedup  (P2-02 audit — pending status also blocks re-send)
# ===========================================================================

class TestPendingDedup:

    def test_pending_record_prevents_resend(self, db_path):
        """A 'pending' delivery for signal_id must suppress a second route_signal call."""
        sig = _make_signal(severity="CRITICAL", signal_id="sig-pending-001")
        config = _tg_config()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        # First call: request succeeds → status becomes 'sent'
        with patch("requests.post", return_value=mock_resp) as mock_post:
            route_signal(sig, config, db_path=db_path)

        # Manually reset to 'pending' to simulate in-flight
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE notification_deliveries SET status='pending', sent_at=NULL "
                "WHERE signal_id=?", (sig.signal_id,)
            )
            conn.commit()
        finally:
            conn.close()

        # Second call: pending record exists → must be skipped
        with patch("requests.post", return_value=mock_resp) as mock_post2:
            result = route_signal(sig, config, db_path=db_path)
        assert result == []
        mock_post2.assert_not_called()

    def test_failed_delivery_allows_retry(self, db_path):
        """A 'failed' delivery for signal_id must allow a retry on the next call."""
        sig = _make_signal(severity="CRITICAL", signal_id="sig-retry-001")
        config = _tg_config()

        # First call: HTTP fails → status 'failed'
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        with patch("requests.post", return_value=fail_resp):
            route_signal(sig, config, db_path=db_path)

        history = get_delivery_history(db_path=db_path)
        assert history[0].status == "failed"

        # Second call: retry allowed because status is 'failed'
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        with patch("requests.post", return_value=ok_resp) as mock_retry:
            route_signal(sig, config, db_path=db_path)
        mock_retry.assert_called_once()


# ===========================================================================
# TestStartupNotificationWiring  (P2-02 audit — startup credential_failure routes)
# ===========================================================================

class TestStartupNotificationWiring:

    def test_startup_credential_failure_routes_notification(self, db_path):
        """startup.py credential_failure emit triggers notification routing."""
        config = _tg_config()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        captured_routes = []

        def capture_route(signal, cfg, db_path=None):
            captured_routes.append(signal.signal_type)
            return []

        with patch("hypogeum.ois.emit_signal") as mock_emit:
            mock_emit.return_value = "sig-startup-test"
            from hypogeum.startup import run_startup_checks
            import hypogeum.openrouter as _or
            original = _or._use_anthropic_for_run
            _or._use_anthropic_for_run = True
            try:
                import os
                os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
                from hypogeum.config import Config
                cfg = Config()
                cfg.ANTHROPIC_API_KEY = None
                with patch("hypogeum.startup.reset_run_state"):
                    with patch("hypogeum.startup.check_health", return_value=False):
                        try:
                            run_startup_checks(cfg, episode_id="ep_wire_test")
                        except Exception:
                            pass
            finally:
                _or._use_anthropic_for_run = original

        # emit_signal was called with credential_failure and the config
        assert mock_emit.called
        call_kwargs = mock_emit.call_args[1] if mock_emit.call_args[1] else {}
        call_args = mock_emit.call_args[0] if mock_emit.call_args[0] else ()
        # signal_type and config must be present
        sig_type = call_kwargs.get("signal_type", call_args[0] if call_args else None)
        passed_config = call_kwargs.get("config")
        assert sig_type == "credential_failure"
        assert passed_config is not None  # config was passed explicitly


# ===========================================================================
# TestFinanceNotificationWiring  (P2-02 audit — budget_exhaustion routes)
# ===========================================================================

class TestFinanceNotificationWiring:

    def test_budget_exhaustion_emit_uses_module_import(self, db_path, tmp_path):
        """finance_agent uses module-level import so patch('ois.emit_signal') works."""
        import os
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        from hypogeum.agents.finance_agent import run_budget_authorization
        from hypogeum.config import Config
        config = Config()
        config.FINANCE_BUDGET_CEILING_USD = 0.001

        script_package = {
            "script": {
                "sections": [
                    {"section_id": f"s{i}", "narration": "A" * 500}
                    for i in range(3)
                ]
            }
        }

        captured = {}

        def capture_emit(**kwargs):
            captured.update(kwargs)
            return "test-signal-id"

        with patch("hypogeum.ois.emit_signal", side_effect=capture_emit):
            run_budget_authorization(
                script_package=script_package,
                episode_id="ep_wiring_test",
                config=config,
                output_dir=tmp_path,
            )

        assert captured.get("signal_type") == "budget_exhaustion"
        assert captured.get("config") is config  # config was passed through

    def test_budget_exhaustion_no_live_network_calls(self, db_path, tmp_path):
        """budget_exhaustion emission must not make real network calls."""
        import os
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        from hypogeum.agents.finance_agent import run_budget_authorization
        from hypogeum.config import Config
        config = Config()
        config.FINANCE_BUDGET_CEILING_USD = 0.001
        config.TELEGRAM_BOT_TOKEN = None  # no channels → no network

        script_package = {
            "script": {
                "sections": [
                    {"section_id": "s0", "narration": "A" * 500}
                ]
            }
        }

        with patch("requests.post") as mock_post:
            with patch("hypogeum.ois.get_connection",
                       side_effect=lambda p=None: get_connection(db_path)):
                run_budget_authorization(
                    script_package=script_package,
                    episode_id="ep_net_test",
                    config=config,
                    output_dir=tmp_path,
                )

        mock_post.assert_not_called()


# ===========================================================================
# Item 1 — run_failure production code verification
# Confirms SIGNAL_TYPES in production ois.py (not just test references) has
# run_failure as CRITICAL blocking.
# ===========================================================================

class TestRunFailureProductionDefinition:

    def test_run_failure_in_signal_types_production(self):
        from hypogeum.ois import SIGNAL_TYPES
        assert "run_failure" in SIGNAL_TYPES, \
            "run_failure missing from production hypogeum/ois.py SIGNAL_TYPES"
        assert SIGNAL_TYPES["run_failure"]["severity"] == "CRITICAL"
        assert SIGNAL_TYPES["run_failure"]["blocking"] is True

    def test_run_failure_not_only_in_tests(self):
        """Import SIGNAL_TYPES at module level from production path and inspect."""
        import importlib
        ois_mod = importlib.import_module("hypogeum.ois")
        st = ois_mod.SIGNAL_TYPES
        assert "run_failure" in st
        assert st["run_failure"] == {"severity": "CRITICAL", "blocking": True}


# ===========================================================================
# Item 2 — routing-level verification
# Patches hypogeum.notifications.route_signal and verifies it is invoked for:
#   • credential_failure emitted by startup.py
#   • budget_exhaustion emitted by finance_agent.py
# No live network calls at any point.
# ===========================================================================

class TestRoutingLevelVerification:

    def test_startup_credential_failure_calls_route_signal(self, db_path):
        """
        startup.py 'unavailable' path → emit_signal(credential_failure) →
        route_signal called with signal.signal_type == 'credential_failure'.
        """
        import os, hypogeum.openrouter as _or
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        from hypogeum.config import Config
        from hypogeum.startup import run_startup_checks

        config = Config()
        config.ANTHROPIC_API_KEY = None         # no fallback → unavailable
        config.TELEGRAM_BOT_TOKEN = "fake-tok"  # channel configured so route is attempted
        config.TELEGRAM_CHAT_ID = "fake-chat"

        routed_types = []

        def capture(signal, cfg, db_path=None):
            routed_types.append(signal.signal_type)
            return []

        original = _or._use_anthropic_for_run
        _or._use_anthropic_for_run = True        # simulate: OR unreachable
        try:
            with patch("hypogeum.notifications.route_signal", side_effect=capture):
                with patch("hypogeum.ois.get_connection",
                           side_effect=lambda p=None: get_connection(db_path)):
                    with patch("hypogeum.startup.reset_run_state"):
                        with patch("hypogeum.startup.check_health",
                                   return_value=False):
                            try:
                                run_startup_checks(config,
                                                   episode_id="ep_route_startup")
                            except Exception:
                                pass
        finally:
            _or._use_anthropic_for_run = original

        assert "credential_failure" in routed_types, (
            f"route_signal was not called with credential_failure; got: {routed_types}"
        )

    def test_finance_budget_exhaustion_calls_route_signal(self, db_path, tmp_path):
        """
        finance_agent BLOCKED verdict → emit_signal(budget_exhaustion) →
        route_signal called with signal.signal_type == 'budget_exhaustion'.
        """
        import os
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        from hypogeum.config import Config
        from hypogeum.agents.finance_agent import run_budget_authorization

        config = Config()
        config.FINANCE_BUDGET_CEILING_USD = 0.001   # guaranteed BLOCKED
        config.TELEGRAM_BOT_TOKEN = "fake-tok"
        config.TELEGRAM_CHAT_ID = "fake-chat"

        routed_types = []

        def capture(signal, cfg, db_path=None):
            routed_types.append(signal.signal_type)
            return []

        script_package = {
            "script": {
                "sections": [
                    {"section_id": f"s{i}", "narration": "A" * 500}
                    for i in range(3)
                ]
            }
        }

        with patch("hypogeum.notifications.route_signal", side_effect=capture):
            with patch("hypogeum.ois.get_connection",
                       side_effect=lambda p=None: get_connection(db_path)):
                run_budget_authorization(
                    script_package=script_package,
                    episode_id="ep_route_finance",
                    config=config,
                    output_dir=tmp_path,
                )

        assert "budget_exhaustion" in routed_types, (
            f"route_signal was not called with budget_exhaustion; got: {routed_types}"
        )

    def test_no_live_network_calls_during_routing_test(self, db_path, tmp_path):
        """Paranoia check: patching route_signal means zero real HTTP/SMTP calls."""
        import os
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        from hypogeum.config import Config
        from hypogeum.agents.finance_agent import run_budget_authorization

        config = Config()
        config.FINANCE_BUDGET_CEILING_USD = 0.001

        with patch("hypogeum.notifications.route_signal", return_value=[]):
            with patch("hypogeum.ois.get_connection",
                       side_effect=lambda p=None: get_connection(db_path)):
                with patch("requests.post") as mock_post:
                    run_budget_authorization(
                        script_package={
                            "script": {"sections": [
                                {"section_id": "s0", "narration": "A" * 500}
                            ]}
                        },
                        episode_id="ep_no_net",
                        config=config,
                        output_dir=tmp_path,
                    )
        mock_post.assert_not_called()


# ===========================================================================
# Item 3 — notification_deliveries migration path verification
# Confirms that an existing DB (created before P2-02) receives the
# notification_deliveries table when initialize_schema() is called again,
# without requiring schema recreation.
# ===========================================================================

class TestNotificationDeliveriesMigration:

    def test_notification_deliveries_created_on_existing_db(self, tmp_path):
        """
        Migration path: DB exists without notification_deliveries →
        initialize_schema() → table appears.

        This is the exact upgrade scenario for pre-P2-02 databases.
        """
        db = tmp_path / "pre_p202.db"
        # Step 1: create DB with full current schema (includes notification_deliveries)
        initialize_schema(db)
        # Step 2: simulate pre-P2-02 by dropping the table
        conn = get_connection(db)
        try:
            conn.execute("DROP TABLE IF EXISTS notification_deliveries")
            conn.commit()
            tables_before = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        finally:
            conn.close()
        assert "notification_deliveries" not in tables_before

        # Step 3: re-run initialize_schema() — simulates next startup after upgrade
        initialize_schema(db)

        # Step 4: table must now exist with all required columns
        conn = get_connection(db)
        try:
            tables_after = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(notification_deliveries)"
            ).fetchall()}
        finally:
            conn.close()

        assert "notification_deliveries" in tables_after
        required_cols = {
            "delivery_id", "signal_id", "channel_name", "recipient",
            "subject", "message", "status", "sent_at", "error", "created_at",
        }
        assert required_cols.issubset(cols), (
            f"Missing columns: {required_cols - cols}"
        )

    def test_initialize_schema_is_idempotent_with_existing_deliveries(self, tmp_path):
        """
        Calling initialize_schema() on a DB that already has
        notification_deliveries must not destroy existing rows.
        """
        db = tmp_path / "idempotent.db"
        initialize_schema(db)
        # Insert a delivery record
        conn = get_connection(db)
        try:
            conn.execute(
                "INSERT INTO notification_deliveries "
                "(delivery_id, channel_name, recipient, message, created_at) "
                "VALUES ('d1', 'telegram', 'chat', 'msg', '2026-06-15T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

        # Re-run initialize_schema() — must be idempotent
        initialize_schema(db)

        conn = get_connection(db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM notification_deliveries WHERE delivery_id='d1'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert n == 1, "initialize_schema() destroyed existing notification_deliveries rows"

    def test_migration_documented_path(self):
        """
        Documents the migration contract explicitly.

        notification_deliveries uses CREATE TABLE IF NOT EXISTS in schema.sql.
        initialize_schema() calls conn.executescript(schema_sql) which runs all
        CREATE TABLE IF NOT EXISTS statements against an existing database.
        This means any pre-P2-02 database gets the table on the very next call
        to initialize_schema() — which happens at every pipeline startup.
        No manual migration step, no schema recreation required.
        """
        import inspect
        from hypogeum import db as _db
        # Follow __wrapped__ so the Phase 3 conftest production-DB guard
        # (which wraps this function per-test) stays transparent here.
        src = inspect.getsource(inspect.unwrap(_db.initialize_schema))
        assert "executescript" in src, "initialize_schema must use executescript"
        assert "CREATE TABLE IF NOT EXISTS" in open(
            __file__.replace("test_notifications.py", "")
            .rstrip("/") + "/../hypogeum/schema.sql"
        ).read()
