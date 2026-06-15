"""
tests/test_ois.py — P2-01: OIS Signal Routing tests

Coverage:
    TestEmitSignal            — create, metadata, deduplication, cooldown
    TestIsBlocked             — factory block checks
    TestIsChannelBlocked      — channel vs factory scope blocking
    TestResolveSignal         — lifecycle transitions
    TestSignalLifecycle       — all four lifecycle states
    TestSignalTypes           — all V1 signal types round-trip
    TestGetActiveSignals      — filter by channel_id and severity
    TestGetBlockingSignals    — filtering and scope
    TestPipelineOISIntegration — pipeline raises OISBlockedError before agents
    TestBudgetExhaustionSignal — finance_agent emits budget_exhaustion
    TestStartupOISIntegration  — startup emits credential_failure
    TestNoNetworkCalls        — no sockets opened
    TestBackwardsCompat       — existing signal ingestion preserved
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.ois import (
    SIGNAL_TYPES,
    OISBlockedError,
    SignalRecord,
    auto_resolve_signal,
    cancel_signal,
    emit_signal,
    get_active_signals,
    get_blocking_signals,
    is_channel_blocked,
    is_factory_blocked,
    resolve_signal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "ois_test.db"
    initialize_schema(p)
    return p


def _emit(db_path, signal_type="run_completed", severity=None,
          message="test message", channel_id=None,
          source_system="test", blocking=None,
          episode_id=None, run_id=None):
    defaults = SIGNAL_TYPES.get(signal_type, {})
    sev = severity if severity is not None else defaults.get("severity", "INFO")
    blk = blocking if blocking is not None else defaults.get("blocking", False)
    return emit_signal(
        signal_type=signal_type,
        severity=sev,
        message=message,
        channel_id=channel_id,
        source_system=source_system,
        blocking=blk,
        episode_id=episode_id,
        run_id=run_id,
        db_path=db_path,
    )


# ===========================================================================
# TestEmitSignal
# ===========================================================================

class TestEmitSignal:

    def test_returns_signal_id_string(self, db_path):
        sid = _emit(db_path)
        assert isinstance(sid, str) and len(sid) > 0

    def test_creates_db_record(self, db_path):
        sid = _emit(db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_severity_stored(self, db_path):
        sid = _emit(db_path, signal_type="budget_exhaustion")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT severity FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["severity"] == "CRITICAL"

    def test_blocking_flag_stored(self, db_path):
        sid = _emit(db_path, signal_type="budget_exhaustion")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT blocking FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["blocking"] == 1

    def test_non_blocking_flag_stored(self, db_path):
        sid = _emit(db_path, signal_type="run_completed")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT blocking FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["blocking"] == 0

    def test_signal_type_stored(self, db_path):
        sid = _emit(db_path, signal_type="emergency_halt")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT signal_type FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["signal_type"] == "emergency_halt"

    def test_source_system_stored(self, db_path):
        sid = _emit(db_path, source_system="pipeline")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT source_system FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["source_system"] == "pipeline"

    def test_channel_id_stored(self, db_path):
        sid = _emit(db_path, channel_id="hypogeum", signal_type="channel_blocked",
                    blocking=True, severity="CRITICAL")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT channel_id FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["channel_id"] == "hypogeum"

    def test_message_in_full_record(self, db_path):
        sid = _emit(db_path, message="Budget exhausted at $5.00")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT full_record FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        data = json.loads(row["full_record"])
        assert data["message"] == "Budget exhausted at $5.00"

    def test_lifecycle_status_defaults_to_open(self, db_path):
        sid = _emit(db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT lifecycle_status FROM signal_records WHERE signal_id = ?",
                (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["lifecycle_status"] == "open"

    def test_deduplication_returns_existing_id(self, db_path):
        sid1 = _emit(db_path, signal_type="budget_exhaustion", severity="CRITICAL",
                     blocking=True, channel_id=None)
        sid2 = _emit(db_path, signal_type="budget_exhaustion", severity="CRITICAL",
                     blocking=True, channel_id=None)
        assert sid1 == sid2

    def test_different_types_not_deduplicated(self, db_path):
        sid1 = _emit(db_path, signal_type="budget_exhaustion", blocking=True,
                     severity="CRITICAL")
        sid2 = _emit(db_path, signal_type="emergency_halt", blocking=True,
                     severity="CRITICAL")
        assert sid1 != sid2

    def test_different_channels_not_deduplicated(self, db_path):
        sid1 = _emit(db_path, signal_type="channel_blocked", channel_id="ch_a",
                     severity="CRITICAL", blocking=True)
        sid2 = _emit(db_path, signal_type="channel_blocked", channel_id="ch_b",
                     severity="CRITICAL", blocking=True)
        assert sid1 != sid2

    def test_factory_scope_signal_no_channel_id(self, db_path):
        sid = _emit(db_path, signal_type="emergency_halt", channel_id=None,
                    severity="CRITICAL", blocking=True)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT channel_id FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["channel_id"] is None


# ===========================================================================
# TestIsBlocked
# ===========================================================================

class TestIsBlocked:

    def test_not_blocked_when_no_signals(self, db_path):
        assert is_factory_blocked(db_path=db_path) is False

    def test_not_blocked_with_non_blocking_signals(self, db_path):
        _emit(db_path, signal_type="run_completed", blocking=False, severity="INFO")
        _emit(db_path, signal_type="run_failed", blocking=False, severity="WARNING")
        assert is_factory_blocked(db_path=db_path) is False

    def test_blocked_when_blocking_signal_exists(self, db_path):
        _emit(db_path, signal_type="budget_exhaustion", blocking=True,
              severity="CRITICAL", channel_id=None)
        assert is_factory_blocked(db_path=db_path) is True

    def test_blocked_emergency_halt(self, db_path):
        _emit(db_path, signal_type="emergency_halt", blocking=True,
              severity="CRITICAL", channel_id=None)
        assert is_factory_blocked(db_path=db_path) is True

    def test_not_blocked_after_resolve(self, db_path):
        sid = _emit(db_path, signal_type="budget_exhaustion", blocking=True,
                    severity="CRITICAL", channel_id=None)
        assert is_factory_blocked(db_path=db_path) is True
        resolve_signal(sid, "operator", db_path=db_path)
        assert is_factory_blocked(db_path=db_path) is False

    def test_channel_scoped_blocking_not_factory_block(self, db_path):
        """A channel-specific blocking signal should NOT block the factory."""
        _emit(db_path, signal_type="channel_blocked", blocking=True,
              severity="CRITICAL", channel_id="hypogeum")
        assert is_factory_blocked(db_path=db_path) is False


# ===========================================================================
# TestIsChannelBlocked
# ===========================================================================

class TestIsChannelBlocked:

    def test_not_blocked_when_no_signals(self, db_path):
        assert is_channel_blocked("hypogeum", db_path=db_path) is False

    def test_factory_scope_signal_blocks_channel(self, db_path):
        _emit(db_path, signal_type="budget_exhaustion", blocking=True,
              severity="CRITICAL", channel_id=None)
        assert is_channel_blocked("hypogeum", db_path=db_path) is True

    def test_channel_specific_signal_blocks_channel(self, db_path):
        _emit(db_path, signal_type="channel_blocked", blocking=True,
              severity="CRITICAL", channel_id="hypogeum")
        assert is_channel_blocked("hypogeum", db_path=db_path) is True

    def test_other_channel_not_blocked(self, db_path):
        _emit(db_path, signal_type="channel_blocked", blocking=True,
              severity="CRITICAL", channel_id="other_channel")
        assert is_channel_blocked("hypogeum", db_path=db_path) is False

    def test_resolved_channel_signal_not_blocking(self, db_path):
        sid = _emit(db_path, signal_type="channel_blocked", blocking=True,
                    severity="CRITICAL", channel_id="hypogeum")
        resolve_signal(sid, "operator", db_path=db_path)
        assert is_channel_blocked("hypogeum", db_path=db_path) is False

    def test_factory_resolved_clears_channel_block(self, db_path):
        sid = _emit(db_path, signal_type="budget_exhaustion", blocking=True,
                    severity="CRITICAL", channel_id=None)
        assert is_channel_blocked("hypogeum", db_path=db_path) is True
        resolve_signal(sid, "operator", db_path=db_path)
        assert is_channel_blocked("hypogeum", db_path=db_path) is False


# ===========================================================================
# TestResolveSignal
# ===========================================================================

class TestResolveSignal:

    def test_resolve_returns_true(self, db_path):
        sid = _emit(db_path)
        assert resolve_signal(sid, "operator", db_path=db_path) is True

    def test_resolve_sets_resolved_at(self, db_path):
        sid = _emit(db_path)
        resolve_signal(sid, "operator", db_path=db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT resolved_at FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["resolved_at"] is not None

    def test_resolve_sets_lifecycle_resolved(self, db_path):
        sid = _emit(db_path)
        resolve_signal(sid, "op", db_path=db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT lifecycle_status FROM signal_records WHERE signal_id = ?",
                (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["lifecycle_status"] == "resolved"

    def test_resolve_stores_resolved_by(self, db_path):
        sid = _emit(db_path)
        resolve_signal(sid, "user-123", db_path=db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT resolved_by FROM signal_records WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["resolved_by"] == "user-123"

    def test_resolved_signal_leaves_factory_unblocked(self, db_path):
        sid = _emit(db_path, signal_type="budget_exhaustion", blocking=True,
                    severity="CRITICAL", channel_id=None)
        resolve_signal(sid, "op", db_path=db_path)
        assert is_factory_blocked(db_path=db_path) is False


# ===========================================================================
# TestSignalLifecycle
# ===========================================================================

class TestSignalLifecycle:

    def test_open_lifecycle(self, db_path):
        sid = _emit(db_path)
        signals = get_active_signals(db_path=db_path)
        assert any(s.signal_id == sid for s in signals)

    def test_resolved_lifecycle(self, db_path):
        sid = _emit(db_path)
        resolve_signal(sid, "op", db_path=db_path)
        signals = get_active_signals(db_path=db_path)
        assert not any(s.signal_id == sid for s in signals)

    def test_auto_resolved_lifecycle(self, db_path):
        sid = _emit(db_path)
        auto_resolve_signal(sid, "budget_replenished", db_path=db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT lifecycle_status, resolved_by FROM signal_records "
                "WHERE signal_id = ?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["lifecycle_status"] == "auto_resolved"
        assert "system:" in row["resolved_by"]

    def test_cancelled_lifecycle(self, db_path):
        sid = _emit(db_path)
        cancel_signal(sid, "operator", db_path=db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT lifecycle_status FROM signal_records WHERE signal_id = ?",
                (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["lifecycle_status"] == "cancelled"

    def test_cancelled_signal_not_active(self, db_path):
        sid = _emit(db_path)
        cancel_signal(sid, "operator", db_path=db_path)
        signals = get_active_signals(db_path=db_path)
        assert not any(s.signal_id == sid for s in signals)


# ===========================================================================
# TestSignalTypes
# ===========================================================================

class TestSignalTypes:
    """All V1 signal types must be emittable and round-trip correctly."""

    @pytest.mark.parametrize("stype", list(SIGNAL_TYPES.keys()))
    def test_signal_type_round_trip(self, db_path, stype):
        defaults = SIGNAL_TYPES[stype]
        sid = emit_signal(
            signal_type=stype,
            severity=defaults["severity"],
            message=f"test {stype}",
            channel_id=None,
            source_system="test",
            blocking=defaults["blocking"],
            db_path=db_path,
        )
        assert isinstance(sid, str)
        signals = get_blocking_signals(db_path=db_path) if defaults["blocking"] \
            else get_active_signals(db_path=db_path)
        found = [s for s in signals if s.signal_id == sid]
        assert len(found) == 1
        assert found[0].signal_type == stype
        assert found[0].blocking == defaults["blocking"]


# ===========================================================================
# TestGetActiveSignals
# ===========================================================================

class TestGetActiveSignals:

    def test_empty_returns_empty_list(self, db_path):
        assert get_active_signals(db_path=db_path) == []

    def test_returns_open_signals_only(self, db_path):
        sid1 = _emit(db_path, signal_type="run_completed", severity="INFO")
        sid2 = _emit(db_path, signal_type="run_failed", severity="WARNING")
        resolve_signal(sid2, "op", db_path=db_path)
        active = get_active_signals(db_path=db_path)
        ids = {s.signal_id for s in active}
        assert sid1 in ids
        assert sid2 not in ids

    def test_filter_by_severity(self, db_path):
        _emit(db_path, signal_type="run_completed", severity="INFO")
        _emit(db_path, signal_type="run_failed", severity="WARNING")
        critical_sid = _emit(db_path, signal_type="budget_exhaustion",
                             severity="CRITICAL", blocking=True)
        critical = get_active_signals(severity="CRITICAL", db_path=db_path)
        assert any(s.signal_id == critical_sid for s in critical)
        assert all(s.severity == "CRITICAL" for s in critical)

    def test_filter_by_channel_id_includes_factory(self, db_path):
        factory_sid = _emit(db_path, signal_type="budget_exhaustion",
                            severity="CRITICAL", blocking=True, channel_id=None)
        channel_sid = _emit(db_path, signal_type="channel_blocked",
                            severity="CRITICAL", blocking=True, channel_id="hypogeum")
        other_sid = _emit(db_path, signal_type="channel_blocked",
                          severity="CRITICAL", blocking=True, channel_id="other")
        results = get_active_signals(channel_id="hypogeum", db_path=db_path)
        ids = {s.signal_id for s in results}
        assert factory_sid in ids
        assert channel_sid in ids
        assert other_sid not in ids


# ===========================================================================
# TestGetBlockingSignals
# ===========================================================================

class TestGetBlockingSignals:

    def test_empty_when_no_blocking(self, db_path):
        _emit(db_path, signal_type="run_completed", blocking=False)
        assert get_blocking_signals(db_path=db_path) == []

    def test_returns_only_blocking(self, db_path):
        blocking_sid = _emit(db_path, signal_type="budget_exhaustion",
                             blocking=True, severity="CRITICAL")
        _emit(db_path, signal_type="run_failed", blocking=False, severity="WARNING")
        signals = get_blocking_signals(db_path=db_path)
        assert len(signals) == 1
        assert signals[0].signal_id == blocking_sid

    def test_resolved_blocking_not_returned(self, db_path):
        sid = _emit(db_path, signal_type="budget_exhaustion",
                    blocking=True, severity="CRITICAL")
        resolve_signal(sid, "op", db_path=db_path)
        assert get_blocking_signals(db_path=db_path) == []

    def test_channel_filter_includes_factory_scope(self, db_path):
        factory_sid = _emit(db_path, signal_type="budget_exhaustion",
                            blocking=True, severity="CRITICAL", channel_id=None)
        ch_sid = _emit(db_path, signal_type="channel_blocked",
                       blocking=True, severity="CRITICAL", channel_id="hypogeum")
        other_sid = _emit(db_path, signal_type="channel_blocked",
                          blocking=True, severity="CRITICAL", channel_id="other")
        results = get_blocking_signals(channel_id="hypogeum", db_path=db_path)
        ids = {s.signal_id for s in results}
        assert factory_sid in ids
        assert ch_sid in ids
        assert other_sid not in ids


# ===========================================================================
# TestPipelineOISIntegration
# ===========================================================================

class TestPipelineOISIntegration:

    def _make_config(self):
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
        from hypogeum.config import Config
        return Config()

    def test_ois_blocked_raises_ois_blocked_error(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        config = self._make_config()
        mock_signals = [MagicMock(signal_type="budget_exhaustion")]
        with patch("hypogeum.pipeline.is_factory_blocked", return_value=True):
            with patch("hypogeum.pipeline.get_blocking_signals",
                       return_value=mock_signals):
                with pytest.raises(OISBlockedError) as exc_info:
                    run_pipeline(
                        topic_input="Test",
                        episode_id="ep_ois_test",
                        config=config,
                        channel_id=None,
                        output_dir=tmp_path,
                    )
        assert exc_info.value.signals == mock_signals

    def test_ois_blocked_raises_before_run_registration(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        config = self._make_config()
        mock_signals = [MagicMock(signal_type="emergency_halt")]
        with patch("hypogeum.pipeline.is_factory_blocked", return_value=True):
            with patch("hypogeum.pipeline.get_blocking_signals",
                       return_value=mock_signals):
                with patch("hypogeum.pipeline.register_run") as mock_register:
                    with pytest.raises(OISBlockedError):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_ois_test",
                            config=config,
                            channel_id=None,
                            output_dir=tmp_path,
                        )
        mock_register.assert_not_called()

    def test_ois_not_blocked_does_not_raise(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        from hypogeum.startup import StartupReport
        config = self._make_config()
        report = StartupReport(
            overall_status="healthy",
            openrouter_reachable=True,
            models_verified=True,
            fallback_active=False,
            fallback_configured=True,
            checked_at="2026-06-15T00:00:00Z",
        )
        with patch("hypogeum.pipeline.is_factory_blocked", return_value=False):
            with patch("hypogeum.pipeline.is_channel_blocked", return_value=False):
                with patch("hypogeum.pipeline.run_startup_checks",
                           return_value=report):
                    with patch("hypogeum.pipeline._run_executive_producer",
                               side_effect=NotImplementedError("stub")):
                        with pytest.raises(NotImplementedError):
                            run_pipeline(
                                topic_input="Test",
                                episode_id="ep_ois_ok",
                                config=config,
                                channel_id=None,
                                output_dir=tmp_path,
                            )

    def test_channel_blocked_raises_before_run_registration(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        from hypogeum.startup import StartupReport
        from hypogeum.channel_config import ChannelConfig, ChannelProfile
        config = self._make_config()
        mock_ch_cfg = MagicMock()
        mock_ch_cfg.channel.status = "active"
        mock_signals = [MagicMock(signal_type="channel_blocked")]

        with patch("hypogeum.channel_config.load_channel_config",
                   return_value=mock_ch_cfg):
            with patch("hypogeum.pipeline.is_factory_blocked", return_value=False):
                with patch("hypogeum.pipeline.is_channel_blocked",
                           return_value=True):
                    with patch("hypogeum.pipeline.get_blocking_signals",
                               return_value=mock_signals):
                        with patch("hypogeum.pipeline.register_run") as mock_reg:
                            with pytest.raises(OISBlockedError):
                                run_pipeline(
                                    topic_input="Test",
                                    episode_id="ep_ch_blocked",
                                    config=config,
                                    channel_id="hypogeum",
                                    output_dir=tmp_path,
                                )
        mock_reg.assert_not_called()

    def test_ois_blocked_error_exports_from_pipeline(self):
        from hypogeum.pipeline import OISBlockedError as PipelineOISBlockedError
        assert PipelineOISBlockedError is OISBlockedError


# ===========================================================================
# TestBudgetExhaustionSignal
# ===========================================================================

class TestBudgetExhaustionSignal:

    def test_budget_blocked_emits_signal(self, db_path, tmp_path):
        """run_budget_authorization BLOCKED verdict emits budget_exhaustion."""
        from hypogeum.agents.finance_agent import run_budget_authorization
        from hypogeum.config import Config
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        config = Config()
        config.FINANCE_BUDGET_CEILING_USD = 0.001  # Tiny ceiling → BLOCKED

        script_package = {
            "script": {
                "sections": [
                    {"section_id": f"s{i}", "narration": "A" * 1000}
                    for i in range(5)
                ]
            }
        }

        with patch("hypogeum.ois.get_connection",
                   side_effect=lambda p=None: get_connection(db_path)):
            run_budget_authorization(
                script_package=script_package,
                episode_id="ep_budget_test",
                config=config,
                output_dir=tmp_path,
            )

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT signal_type, blocking FROM signal_records "
                "WHERE signal_type = 'budget_exhaustion'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["blocking"] == 1

    def test_budget_blocked_signal_is_critical(self, db_path, tmp_path):
        from hypogeum.agents.finance_agent import run_budget_authorization
        from hypogeum.config import Config
        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        config = Config()
        config.FINANCE_BUDGET_CEILING_USD = 0.001

        script_package = {
            "script": {
                "sections": [
                    {"section_id": "s0", "narration": "A" * 2000}
                ]
            }
        }

        with patch("hypogeum.ois.get_connection",
                   side_effect=lambda p=None: get_connection(db_path)):
            run_budget_authorization(
                script_package=script_package,
                episode_id="ep_severity_test",
                config=config,
                output_dir=tmp_path,
            )

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT severity FROM signal_records "
                "WHERE signal_type = 'budget_exhaustion'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["severity"] == "CRITICAL"


# ===========================================================================
# TestStartupOISIntegration
# ===========================================================================

class TestStartupOISIntegration:

    def test_credential_failure_emitted_on_startup_unavailable(self):
        """startup emits credential_failure signal when API path is unavailable."""
        from hypogeum.startup import run_startup_checks
        from hypogeum.config import Config
        import hypogeum.openrouter as _or

        os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")
        config = Config()
        config.ANTHROPIC_API_KEY = None  # no fallback key

        captured_signals = []

        def capture_emit(**kwargs):
            captured_signals.append(kwargs)
            return "test-signal-id"

        original_flag = _or._use_anthropic_for_run
        _or._use_anthropic_for_run = True  # simulate: check_health set this
        try:
            with patch("hypogeum.ois.emit_signal", capture_emit):
                # Suppress the reset so our flag survives; mock check_health noop
                with patch("hypogeum.startup.reset_run_state"):
                    with patch("hypogeum.startup.check_health", return_value=False):
                        try:
                            run_startup_checks(config, episode_id="ep_startup_test")
                        except Exception:
                            pass
        finally:
            _or._use_anthropic_for_run = original_flag

        assert any(
            s.get("signal_type") == "credential_failure"
            for s in captured_signals
        )


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    def test_emit_opens_no_sockets(self, db_path, monkeypatch):
        def no_socket(*a, **k):
            raise AssertionError("OIS opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        _emit(db_path)

    def test_is_factory_blocked_opens_no_sockets(self, db_path, monkeypatch):
        _emit(db_path, signal_type="budget_exhaustion", blocking=True,
              severity="CRITICAL")
        def no_socket(*a, **k):
            raise AssertionError("OIS opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        is_factory_blocked(db_path=db_path)

    def test_resolve_opens_no_sockets(self, db_path, monkeypatch):
        sid = _emit(db_path)
        def no_socket(*a, **k):
            raise AssertionError("OIS opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        resolve_signal(sid, "op", db_path=db_path)


# ===========================================================================
# TestBackwardsCompat
# ===========================================================================

class TestBackwardsCompat:

    def test_signals_ingest_still_works(self, tmp_path):
        """Existing analytics signal ingestion must be unaffected."""
        from hypogeum.signals import ingest_signal, list_signal_records
        record = ingest_signal(
            episode_id="ep_compat_test",
            analytics_snapshot={"status": "captured", "metrics": {"views": 100},
                                 "window_days": 7, "youtube_video_id": "abc123"},
            output_dir=tmp_path,
        )
        assert record["data_quality"] == "captured"
        records = list_signal_records(signals_dir=tmp_path)
        assert len(records) == 1

    def test_ois_re_exports_from_signals(self):
        """OIS functions re-exported from signals.py are the same objects."""
        from hypogeum import signals as s
        from hypogeum import ois
        assert s.emit_signal is ois.emit_signal
        assert s.is_factory_blocked is ois.is_factory_blocked
        assert s.OISBlockedError is ois.OISBlockedError


# ===========================================================================
# TestRunFailureBlockingType  (P2-02 audit — run_failure must be CRITICAL/blocking)
# ===========================================================================

class TestRunFailureBlockingType:

    def test_run_failure_in_signal_types(self):
        assert "run_failure" in SIGNAL_TYPES
        assert SIGNAL_TYPES["run_failure"]["blocking"] is True
        assert SIGNAL_TYPES["run_failure"]["severity"] == "CRITICAL"

    def test_emit_run_failure_blocks_factory(self, db_path):
        _emit(db_path, signal_type="run_failure", blocking=True, severity="CRITICAL",
              channel_id=None)
        assert is_factory_blocked(db_path=db_path) is True

    def test_emit_run_failure_round_trip(self, db_path):
        sid = emit_signal(
            signal_type="run_failure",
            severity="CRITICAL",
            message="Pipeline crashed",
            channel_id=None,
            source_system="pipeline",
            blocking=True,
            db_path=db_path,
        )
        blocking = get_blocking_signals(db_path=db_path)
        found = [s for s in blocking if s.signal_id == sid]
        assert len(found) == 1
        assert found[0].signal_type == "run_failure"
        assert found[0].blocking is True

    def test_run_failure_resolved_unblocks_factory(self, db_path):
        sid = _emit(db_path, signal_type="run_failure", blocking=True,
                    severity="CRITICAL", channel_id=None)
        resolve_signal(sid, "operator", db_path=db_path)
        assert is_factory_blocked(db_path=db_path) is False


# ===========================================================================
# TestAllRequiredBlockingTypes  (P2-02 audit — all five blocking types verified)
# ===========================================================================

class TestAllRequiredBlockingTypes:

    @pytest.mark.parametrize("stype", [
        "emergency_halt",
        "budget_exhaustion",
        "credential_failure",
        "channel_blocked",
        "run_failure",
    ])
    def test_required_type_is_blocking(self, stype):
        assert stype in SIGNAL_TYPES, f"{stype} missing from SIGNAL_TYPES"
        assert SIGNAL_TYPES[stype]["blocking"] is True
        assert SIGNAL_TYPES[stype]["severity"] == "CRITICAL"

    @pytest.mark.parametrize("stype", [
        "emergency_halt",
        "budget_exhaustion",
        "credential_failure",
        "channel_blocked",
        "run_failure",
    ])
    def test_required_type_blocks_factory(self, db_path, stype):
        _emit(db_path, signal_type=stype, blocking=True, severity="CRITICAL",
              channel_id=None)
        assert is_factory_blocked(db_path=db_path) is True


# ===========================================================================
# TestEmitAutoConfigRouting  (P2-02 audit — emit_signal routes without explicit config)
# ===========================================================================

class TestEmitAutoConfigRouting:

    def test_emit_without_config_does_not_raise(self, db_path):
        """emit_signal() without config must not raise even if Config() fails."""
        sid = emit_signal(
            signal_type="run_completed",
            severity="INFO",
            message="Done",
            channel_id=None,
            source_system="pipeline",
            blocking=False,
            db_path=db_path,
            # no config argument
        )
        assert isinstance(sid, str)

    def test_emit_with_explicit_config_routes(self, db_path):
        """emit_signal() with explicit config routes notification."""
        from tests.test_notifications import _tg_config
        config = _tg_config()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        from unittest.mock import patch as _patch
        with _patch("requests.post", return_value=mock_resp) as mock_post:
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
        mock_post.assert_called_once()
