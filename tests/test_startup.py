"""
tests/test_startup.py — Unit tests for hypogeum/startup.py

All check_health(), reset_run_state(), _check_ffmpeg(), and _check_youtube_oauth()
calls are mocked. No real network calls are made. Module-level openrouter state is
managed explicitly per test.

Test coverage:
  TestHealthyPath             — check_health True → "healthy" report, correct fields
  TestDegradedModelsMissing   — check_health False, flag not set → "degraded", warnings present
  TestDegradedFallbackActive  — check_health False, flag set, key configured → "degraded"
  TestUnavailable             — check_health False, flag set, no key → "unavailable" → StartupError
  TestStartupReportFields     — all StartupReport fields populated correctly
  TestLogging                 — correct log events at correct levels for each outcome
  TestStateReset              — reset_run_state() called before check_health()
  TestStartupError            — exception has .report attached, message is informative
  TestFfmpegCheck             — ffmpeg missing → error → StartupError with actionable message
  TestYouTubeOAuthCheck       — token valid → healthy; invalid → error; creds absent → skip/warn
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypogeum.openrouter as _openrouter
from hypogeum.config import Config
from hypogeum.startup import StartupError, StartupReport, run_startup_checks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(*, with_anthropic_key: bool = True) -> Config:
    """Build a Config with minimal valid environment."""
    pipeline_vars = [
        "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
        "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
        "ANTHROPIC_API_KEY",
        "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "PERPLEXITY_MAX_TOKENS",
        "MUAPI_API_KEY", "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS", "MUAPI_WEBHOOK_URL",
        "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
        "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST",
        "ELEVENLABS_MUSIC_MODEL_ID", "ELEVENLABS_MUSIC_DEFAULT_DURATION",
        "OPENAI_API_KEY", "WHISPER_MODEL",
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
        "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID",
        "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
        "META_INSTAGRAM_ACCOUNT_ID", "META_FACEBOOK_PAGE_ID",
        "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
    ]
    from unittest.mock import patch as _patch
    original = {k: os.environ.get(k) for k in pipeline_vars}
    for k in pipeline_vars:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    if with_anthropic_key:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    # Patch _load_dotenv so the real .env file does not re-populate cleared vars
    with _patch("hypogeum.config._load_dotenv"):
        try:
            return Config()
        finally:
            for k in pipeline_vars:
                os.environ.pop(k, None)
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v


@pytest.fixture(autouse=True)
def reset_openrouter_state():
    """Reset openrouter module flag before and after each test."""
    _openrouter.reset_run_state()
    yield
    _openrouter.reset_run_state()


def run_checks_with(
    *,
    health_result: bool,
    flag_value: bool = False,
    with_anthropic_key: bool = True,
    episode_id: str = "ep_001",
    ffmpeg_ok: bool = True,
    youtube_token_result: tuple = (True, None),
):
    """
    Run run_startup_checks() with mocked check_health() and controlled flag state.

    check_health is patched to return health_result.
    _use_anthropic_for_run flag is set to flag_value to simulate what check_health()
    would have set (in real usage check_health sets the flag as a side effect).
    _check_ffmpeg is patched to return ffmpeg_ok (default True).
    _check_youtube_oauth is patched to return youtube_token_result (default (True, None)).
    """
    cfg = make_config(with_anthropic_key=with_anthropic_key)

    def fake_check_health(config, episode_id=None, agent=None):
        # Simulate check_health's side effect of setting the module flag.
        _openrouter._use_anthropic_for_run = flag_value
        return health_result

    with patch("hypogeum.startup._check_ffmpeg", return_value=ffmpeg_ok):
        with patch("hypogeum.startup._check_youtube_oauth", return_value=youtube_token_result):
            with patch("hypogeum.startup.check_health", side_effect=fake_check_health):
                with patch("hypogeum.startup.reset_run_state") as mock_reset:
                    return run_startup_checks(cfg, episode_id=episode_id), mock_reset


# ---------------------------------------------------------------------------
# TestHealthyPath
# ---------------------------------------------------------------------------

class TestHealthyPath:
    def test_returns_report(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert isinstance(report, StartupReport)

    def test_overall_status_healthy(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert report.overall_status == "healthy"

    def test_openrouter_reachable_true(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert report.openrouter_reachable is True

    def test_models_verified_true(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert report.models_verified is True

    def test_fallback_active_false(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert report.fallback_active is False

    def test_fallback_configured_true_when_key_present(self):
        report, _ = run_checks_with(health_result=True, flag_value=False, with_anthropic_key=True)
        assert report.fallback_configured is True

    def test_fallback_configured_false_when_key_absent(self):
        report, _ = run_checks_with(health_result=True, flag_value=False, with_anthropic_key=False)
        assert report.fallback_configured is False

    def test_no_warnings(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert report.warnings == []

    def test_no_errors(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert report.errors == []

    def test_checked_at_is_utc_iso8601(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert report.checked_at.endswith("Z")
        assert "T" in report.checked_at

    def test_does_not_raise(self):
        # Should not raise anything on healthy path.
        run_checks_with(health_result=True, flag_value=False)


# ---------------------------------------------------------------------------
# TestDegradedModelsMissing
# ---------------------------------------------------------------------------

class TestDegradedModelsMissing:
    """OpenRouter reachable but one or both models not in model list."""

    def test_overall_status_degraded(self):
        # flag_value=False: OpenRouter is up but check_health found models missing.
        report, _ = run_checks_with(health_result=False, flag_value=False)
        assert report.overall_status == "degraded"

    def test_openrouter_reachable_true(self):
        report, _ = run_checks_with(health_result=False, flag_value=False)
        assert report.openrouter_reachable is True

    def test_models_verified_false(self):
        report, _ = run_checks_with(health_result=False, flag_value=False)
        assert report.models_verified is False

    def test_fallback_active_false(self):
        report, _ = run_checks_with(health_result=False, flag_value=False)
        assert report.fallback_active is False

    def test_warning_present(self):
        report, _ = run_checks_with(health_result=False, flag_value=False)
        assert len(report.warnings) >= 1

    def test_warning_mentions_models(self):
        report, _ = run_checks_with(health_result=False, flag_value=False)
        assert any("model" in w.lower() for w in report.warnings)

    def test_no_errors(self):
        report, _ = run_checks_with(health_result=False, flag_value=False)
        assert report.errors == []

    def test_does_not_raise(self):
        run_checks_with(health_result=False, flag_value=False)


# ---------------------------------------------------------------------------
# TestDegradedFallbackActive
# ---------------------------------------------------------------------------

class TestDegradedFallbackActive:
    """OpenRouter unreachable but Anthropic fallback key is configured."""

    def test_overall_status_degraded(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert report.overall_status == "degraded"

    def test_openrouter_reachable_false(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert report.openrouter_reachable is False

    def test_models_verified_false(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert report.models_verified is False

    def test_fallback_active_true(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert report.fallback_active is True

    def test_fallback_configured_true(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert report.fallback_configured is True

    def test_warning_present(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert len(report.warnings) >= 1

    def test_warning_mentions_anthropic_or_fallback(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        combined = " ".join(report.warnings).lower()
        assert "anthropic" in combined or "fallback" in combined

    def test_no_errors(self):
        report, _ = run_checks_with(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert report.errors == []

    def test_does_not_raise(self):
        run_checks_with(health_result=False, flag_value=True, with_anthropic_key=True)


# ---------------------------------------------------------------------------
# TestUnavailable
# ---------------------------------------------------------------------------

class TestUnavailable:
    """OpenRouter unreachable AND no Anthropic fallback key."""

    def test_raises_startup_error(self):
        with pytest.raises(StartupError):
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )

    def test_startup_error_has_report(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )
        assert isinstance(exc_info.value.report, StartupReport)

    def test_report_overall_status_unavailable(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )
        assert exc_info.value.report.overall_status == "unavailable"

    def test_report_openrouter_reachable_false(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )
        assert exc_info.value.report.openrouter_reachable is False

    def test_report_fallback_configured_false(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )
        assert exc_info.value.report.fallback_configured is False

    def test_report_errors_present(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )
        assert len(exc_info.value.report.errors) >= 1

    def test_error_message_is_informative(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )
        msg = str(exc_info.value).lower()
        # Should mention either the problem or the remedy
        assert any(word in msg for word in ["unreachable", "unavailable", "openrouter", "api"])

    def test_report_warnings_empty(self):
        # On unavailable, we populate errors not warnings.
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(
                health_result=False, flag_value=True, with_anthropic_key=False
            )
        assert exc_info.value.report.warnings == []


# ---------------------------------------------------------------------------
# TestStartupReportFields
# ---------------------------------------------------------------------------

class TestStartupReportFields:
    """Verify all StartupReport fields are set correctly on healthy path."""

    def test_all_required_fields_present(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert hasattr(report, "overall_status")
        assert hasattr(report, "openrouter_reachable")
        assert hasattr(report, "models_verified")
        assert hasattr(report, "fallback_active")
        assert hasattr(report, "fallback_configured")
        assert hasattr(report, "checked_at")
        assert hasattr(report, "warnings")
        assert hasattr(report, "errors")
        assert hasattr(report, "ffmpeg_available")
        assert hasattr(report, "youtube_token_valid")

    def test_checked_at_format(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        # Must match: YYYY-MM-DDTHH:MM:SS.xxxxxxZ
        parts = report.checked_at.split("T")
        assert len(parts) == 2
        assert len(parts[0]) == 10  # date portion
        assert parts[1].endswith("Z")

    def test_warnings_is_list(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert isinstance(report.warnings, list)

    def test_errors_is_list(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert isinstance(report.errors, list)

    def test_overall_status_values(self):
        healthy_report, _ = run_checks_with(health_result=True, flag_value=False)
        assert healthy_report.overall_status in ("healthy", "degraded", "unavailable")

    def test_booleans_are_actual_bools(self):
        report, _ = run_checks_with(health_result=True, flag_value=False)
        assert type(report.openrouter_reachable) is bool
        assert type(report.models_verified) is bool
        assert type(report.fallback_active) is bool
        assert type(report.fallback_configured) is bool


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    """Verify log events are emitted at the correct level for each outcome."""

    def _capture_log(self, *, health_result, flag_value, with_anthropic_key=True,
                     ffmpeg_ok=True, youtube_token_result=(True, None)):
        """
        Run startup checks and capture all log events emitted.
        Returns list of (method_name, event_name) tuples.
        """
        cfg = make_config(with_anthropic_key=with_anthropic_key)
        log_calls = []

        def make_mock_logger(**kwargs):
            mock_log = MagicMock()
            for level in ("info", "warn", "error", "debug"):
                def make_recorder(lvl):
                    def recorder(event, **fields):
                        log_calls.append((lvl, event))
                    return recorder
                getattr(mock_log, level).side_effect = make_recorder(level)
            return mock_log

        def fake_check_health(config, episode_id=None, agent=None):
            _openrouter._use_anthropic_for_run = flag_value
            return health_result

        with patch("hypogeum.startup._check_ffmpeg", return_value=ffmpeg_ok):
            with patch("hypogeum.startup._check_youtube_oauth", return_value=youtube_token_result):
                with patch("hypogeum.startup.get_logger", side_effect=make_mock_logger):
                    with patch("hypogeum.startup.check_health", side_effect=fake_check_health):
                        with patch("hypogeum.startup.reset_run_state"):
                            try:
                                run_startup_checks(cfg, episode_id="ep_log")
                            except StartupError:
                                pass  # unavailable path raises — still capture logs

        return log_calls

    def test_healthy_emits_started(self):
        calls = self._capture_log(health_result=True, flag_value=False)
        assert ("info", "startup_checks_started") in calls

    def test_healthy_emits_completed_at_info(self):
        calls = self._capture_log(health_result=True, flag_value=False)
        assert ("info", "startup_checks_completed") in calls

    def test_healthy_no_warn_or_error_events(self):
        calls = self._capture_log(health_result=True, flag_value=False)
        assert not any(lvl in ("warn", "error") for lvl, _ in calls)

    def test_degraded_models_emits_started(self):
        calls = self._capture_log(health_result=False, flag_value=False)
        assert ("info", "startup_checks_started") in calls

    def test_degraded_models_emits_completed_at_warn(self):
        calls = self._capture_log(health_result=False, flag_value=False)
        assert ("warn", "startup_checks_completed") in calls

    def test_degraded_fallback_emits_completed_at_warn(self):
        calls = self._capture_log(
            health_result=False, flag_value=True, with_anthropic_key=True
        )
        assert ("warn", "startup_checks_completed") in calls

    def test_unavailable_emits_started(self):
        calls = self._capture_log(
            health_result=False, flag_value=True, with_anthropic_key=False
        )
        assert ("info", "startup_checks_started") in calls

    def test_unavailable_emits_failed_at_error(self):
        calls = self._capture_log(
            health_result=False, flag_value=True, with_anthropic_key=False
        )
        assert ("error", "startup_checks_failed") in calls

    def test_unavailable_no_completed_event(self):
        calls = self._capture_log(
            health_result=False, flag_value=True, with_anthropic_key=False
        )
        assert not any(event == "startup_checks_completed" for _, event in calls)

    def test_started_emitted_before_completed(self):
        calls = self._capture_log(health_result=True, flag_value=False)
        events = [event for _, event in calls]
        started_idx = events.index("startup_checks_started")
        completed_idx = events.index("startup_checks_completed")
        assert started_idx < completed_idx


# ---------------------------------------------------------------------------
# TestStateReset
# ---------------------------------------------------------------------------

class TestStateReset:
    """Verify reset_run_state() is called before check_health()."""

    def test_reset_called_once(self):
        _, mock_reset = run_checks_with(health_result=True, flag_value=False)
        assert mock_reset.call_count == 1

    def test_reset_called_before_check_health(self):
        """
        Ensure reset happens before check_health by verifying that the module
        flag is False when entering check_health (even if it was True before).
        """
        cfg = make_config()
        call_order = []

        def fake_reset():
            call_order.append("reset")

        def fake_check_health(config, episode_id=None, agent=None):
            call_order.append("check_health")
            return True

        with patch("hypogeum.startup.reset_run_state", side_effect=fake_reset):
            with patch("hypogeum.startup.check_health", side_effect=fake_check_health):
                run_startup_checks(cfg)

        assert call_order[0] == "reset"
        assert call_order[1] == "check_health"

    def test_reset_called_on_unavailable_path(self):
        """reset_run_state() is always called, even on the failure path."""
        cfg = make_config(with_anthropic_key=False)
        mock_reset = MagicMock()

        def fake_check_health(config, episode_id=None, agent=None):
            _openrouter._use_anthropic_for_run = True
            return False

        with patch("hypogeum.startup.reset_run_state", mock_reset):
            with patch("hypogeum.startup.check_health", side_effect=fake_check_health):
                with pytest.raises(StartupError):
                    run_startup_checks(cfg)

        assert mock_reset.call_count == 1

    def test_reset_with_episode_id(self):
        """episode_id parameter is forwarded but does not affect reset."""
        _, mock_reset = run_checks_with(
            health_result=True, flag_value=False, episode_id="ep_xyz"
        )
        assert mock_reset.call_count == 1


# ---------------------------------------------------------------------------
# TestStartupError
# ---------------------------------------------------------------------------

class TestStartupError:
    def test_is_runtime_error(self):
        assert issubclass(StartupError, RuntimeError)

    def test_has_report_attribute(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(health_result=False, flag_value=True, with_anthropic_key=False)
        assert hasattr(exc_info.value, "report")

    def test_report_is_startup_report(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(health_result=False, flag_value=True, with_anthropic_key=False)
        assert isinstance(exc_info.value.report, StartupReport)

    def test_str_representation_is_not_empty(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(health_result=False, flag_value=True, with_anthropic_key=False)
        assert len(str(exc_info.value)) > 0

    def test_report_checked_at_populated(self):
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(health_result=False, flag_value=True, with_anthropic_key=False)
        assert exc_info.value.report.checked_at.endswith("Z")

    def test_direct_construction(self):
        """StartupError can be constructed directly with a message and report."""
        report = StartupReport(
            overall_status="unavailable",
            openrouter_reachable=False,
            models_verified=False,
            fallback_active=True,
            fallback_configured=False,
            checked_at="2026-06-01T10:00:00.000000Z",
        )
        err = StartupError("test error", report=report)
        assert err.report is report
        assert "test error" in str(err)


# ---------------------------------------------------------------------------
# TestFfmpegCheck
# ---------------------------------------------------------------------------

class TestFfmpegCheck:
    """ffmpeg binary availability check — _check_ffmpeg() and its impact on startup."""

    def _run_with_ffmpeg(self, *, ffmpeg_found: bool):
        """Run startup checks with a controlled ffmpeg presence value."""
        return run_checks_with(
            health_result=True,
            flag_value=False,
            ffmpeg_ok=ffmpeg_found,
        )

    def test_ffmpeg_present_report_field_true(self):
        report, _ = self._run_with_ffmpeg(ffmpeg_found=True)
        assert report.ffmpeg_available is True

    def test_ffmpeg_present_no_errors(self):
        report, _ = self._run_with_ffmpeg(ffmpeg_found=True)
        assert report.errors == []

    def test_ffmpeg_present_status_healthy(self):
        report, _ = self._run_with_ffmpeg(ffmpeg_found=True)
        assert report.overall_status == "healthy"

    def test_ffmpeg_missing_report_field_false(self):
        with pytest.raises(StartupError) as exc_info:
            self._run_with_ffmpeg(ffmpeg_found=False)
        assert exc_info.value.report.ffmpeg_available is False

    def test_ffmpeg_missing_raises_startup_error(self):
        with pytest.raises(StartupError):
            self._run_with_ffmpeg(ffmpeg_found=False)

    def test_ffmpeg_missing_report_unavailable(self):
        with pytest.raises(StartupError) as exc_info:
            self._run_with_ffmpeg(ffmpeg_found=False)
        assert exc_info.value.report.overall_status == "unavailable"

    def test_ffmpeg_missing_error_message_actionable(self):
        with pytest.raises(StartupError) as exc_info:
            self._run_with_ffmpeg(ffmpeg_found=False)
        msg = str(exc_info.value).lower()
        assert "ffmpeg" in msg

    def test_ffmpeg_missing_error_message_mentions_install(self):
        with pytest.raises(StartupError) as exc_info:
            self._run_with_ffmpeg(ffmpeg_found=False)
        msg = str(exc_info.value).lower()
        assert "install" in msg or "path" in msg

    def test_ffmpeg_missing_report_errors_list_nonempty(self):
        with pytest.raises(StartupError) as exc_info:
            self._run_with_ffmpeg(ffmpeg_found=False)
        assert len(exc_info.value.report.errors) >= 1

    def test_ffmpeg_missing_error_mentions_assembly(self):
        """Error should mention what requires ffmpeg so operator understands impact."""
        with pytest.raises(StartupError) as exc_info:
            self._run_with_ffmpeg(ffmpeg_found=False)
        combined = " ".join(exc_info.value.report.errors).lower()
        assert "assembly" in combined or "render" in combined or "video" in combined

    def test_ffmpeg_missing_overrides_healthy_api_path(self):
        """Even when OpenRouter is healthy, ffmpeg missing should produce unavailable."""
        with pytest.raises(StartupError) as exc_info:
            run_checks_with(health_result=True, flag_value=False, ffmpeg_ok=False)
        assert exc_info.value.report.overall_status == "unavailable"
        assert exc_info.value.report.openrouter_reachable is True

    def test_check_ffmpeg_uses_shutil_which(self):
        """_check_ffmpeg must delegate to shutil.which, not subprocess or os.system."""
        from hypogeum.startup import _check_ffmpeg
        with patch("hypogeum.startup.shutil.which", return_value="/usr/bin/ffmpeg") as mock_which:
            result = _check_ffmpeg()
        mock_which.assert_called_once_with("ffmpeg")
        assert result is True

    def test_check_ffmpeg_returns_false_when_which_returns_none(self):
        from hypogeum.startup import _check_ffmpeg
        with patch("hypogeum.startup.shutil.which", return_value=None):
            result = _check_ffmpeg()
        assert result is False


# ---------------------------------------------------------------------------
# TestYouTubeOAuthCheck
# ---------------------------------------------------------------------------

def make_config_with_youtube(*, all_three: bool = True, partial: bool = False) -> "Config":
    """
    Build a Config with controlled YouTube credential state.

    Overrides YouTube attributes directly on the Config object after construction
    to avoid fighting with _load_dotenv() which would restore values from .env.

    all_three=True  → CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN all set.
    partial=True    → only CLIENT_ID set (simulate partial config).
    all_three=False, partial=False → all three are None.
    """
    cfg = make_config(with_anthropic_key=True)
    # Force YouTube attrs to a known state regardless of what .env loaded.
    cfg.YOUTUBE_CLIENT_ID = None
    cfg.YOUTUBE_CLIENT_SECRET = None
    cfg.YOUTUBE_REFRESH_TOKEN = None
    cfg.YOUTUBE_CHANNEL_ID = None
    if all_three:
        cfg.YOUTUBE_CLIENT_ID = "client-id-test"
        cfg.YOUTUBE_CLIENT_SECRET = "client-secret-test"
        cfg.YOUTUBE_REFRESH_TOKEN = "refresh-token-test"
    elif partial:
        cfg.YOUTUBE_CLIENT_ID = "client-id-only"
    return cfg


def run_checks_with_youtube(
    *,
    health_result: bool = True,
    flag_value: bool = False,
    youtube_creds: str = "all",   # "all" | "partial" | "none"
    token_ok: bool = True,
    token_err: str = "token error",
):
    """
    Run run_startup_checks() with YouTube credential scenarios and mocked network.

    youtube_creds controls which credentials are in Config.
    token_ok / token_err control what _check_youtube_oauth returns.
    """
    if youtube_creds == "all":
        cfg = make_config_with_youtube(all_three=True)
    elif youtube_creds == "partial":
        cfg = make_config_with_youtube(all_three=False, partial=True)
    else:
        cfg = make_config_with_youtube(all_three=False, partial=False)

    token_result = (True, None) if token_ok else (False, token_err)

    def fake_check_health(config, episode_id=None, agent=None):
        _openrouter._use_anthropic_for_run = flag_value
        return health_result

    with patch("hypogeum.startup._check_ffmpeg", return_value=True):
        with patch("hypogeum.startup._check_youtube_oauth", return_value=token_result) as mock_yt:
            with patch("hypogeum.startup.check_health", side_effect=fake_check_health):
                with patch("hypogeum.startup.reset_run_state"):
                    try:
                        report = run_startup_checks(cfg)
                        return report, mock_yt, None
                    except StartupError as exc:
                        return exc.report, mock_yt, exc


class TestYouTubeOAuthCheck:
    """YouTube OAuth token exchange preflight check."""

    # --- Creds absent: check not attempted ---

    def test_no_creds_youtube_token_valid_is_none(self):
        report, mock_yt, exc = run_checks_with_youtube(youtube_creds="none")
        assert exc is None
        assert report.youtube_token_valid is None

    def test_no_creds_oauth_helper_not_called(self):
        _, mock_yt, exc = run_checks_with_youtube(youtube_creds="none")
        assert exc is None
        mock_yt.assert_not_called()

    def test_no_creds_no_errors(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="none")
        assert exc is None
        assert report.errors == []

    def test_no_creds_status_healthy(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="none")
        assert exc is None
        assert report.overall_status == "healthy"

    # --- Partial creds: check not attempted, warning emitted ---

    def test_partial_creds_oauth_helper_not_called(self):
        _, mock_yt, exc = run_checks_with_youtube(youtube_creds="partial")
        assert exc is None
        mock_yt.assert_not_called()

    def test_partial_creds_youtube_token_valid_is_none(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="partial")
        assert exc is None
        assert report.youtube_token_valid is None

    def test_partial_creds_warning_present(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="partial")
        assert exc is None
        assert len(report.warnings) >= 1

    def test_partial_creds_warning_mentions_youtube(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="partial")
        assert exc is None
        combined = " ".join(report.warnings).lower()
        assert "youtube" in combined

    def test_partial_creds_no_errors(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="partial")
        assert exc is None
        assert report.errors == []

    # --- All creds present, token valid ---

    def test_valid_token_youtube_token_valid_true(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=True)
        assert exc is None
        assert report.youtube_token_valid is True

    def test_valid_token_oauth_helper_called_once(self):
        _, mock_yt, exc = run_checks_with_youtube(youtube_creds="all", token_ok=True)
        assert exc is None
        mock_yt.assert_called_once()

    def test_valid_token_no_errors(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=True)
        assert exc is None
        assert report.errors == []

    def test_valid_token_status_healthy(self):
        report, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=True)
        assert exc is None
        assert report.overall_status == "healthy"

    # --- All creds present, token invalid ---
    # YouTube OAuth failure is non-blocking (warning, not error) as of 2026-06-15.
    # publishing_agent.py handles youtube_available=False gracefully by skipping
    # all YouTube uploads. Generation stages are unaffected by an expired token.

    def test_invalid_token_does_not_raise_startup_error(self):
        """Invalid YouTube token must NOT raise StartupError — it is a warning only."""
        _, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=False)
        assert exc is None

    def test_invalid_token_youtube_token_valid_false(self):
        """youtube_token_valid is still tracked as False even though it is non-blocking."""
        report, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=False)
        assert exc is None
        assert report.youtube_token_valid is False

    def test_invalid_token_report_not_unavailable(self):
        """With non-blocking YouTube, overall_status stays healthy (OpenRouter is up)."""
        report, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=False)
        assert exc is None
        assert report.overall_status != "unavailable"

    def test_invalid_token_warning_actionable(self):
        """Warning message must mention youtube, oauth, or token so operator knows what failed."""
        report, _, exc = run_checks_with_youtube(
            youtube_creds="all", token_ok=False, token_err="HTTP 401 Unauthorized"
        )
        assert exc is None
        combined = " ".join(report.warnings).lower()
        assert "youtube" in combined or "oauth" in combined or "token" in combined

    def test_invalid_token_warning_mentions_remedy(self):
        """Warning must direct operator to re-authorize."""
        report, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=False)
        assert exc is None
        combined = " ".join(report.warnings).lower()
        assert "re-authoriz" in combined or "refresh_token" in combined or "oauth" in combined

    def test_invalid_token_warning_not_error(self):
        """YouTube token failure must appear in warnings[], not errors[]."""
        report, _, exc = run_checks_with_youtube(youtube_creds="all", token_ok=False)
        assert exc is None
        assert len(report.warnings) >= 1
        assert report.errors == []

    def test_invalid_token_warning_includes_diagnostic(self):
        """The original token error string must appear somewhere in warnings."""
        report, _, exc = run_checks_with_youtube(
            youtube_creds="all", token_ok=False, token_err="HTTP 401 token expired"
        )
        assert exc is None
        combined = " ".join(report.warnings)
        assert "HTTP 401 token expired" in combined

    def test_invalid_token_healthy_api_path_unaffected(self):
        """Invalid YouTube token does not degrade the pipeline when OpenRouter is healthy."""
        report, _, exc = run_checks_with_youtube(
            youtube_creds="all", token_ok=False, health_result=True, flag_value=False
        )
        assert exc is None
        assert report.openrouter_reachable is True
        assert report.overall_status in ("healthy", "degraded")
        assert len(report.warnings) >= 1

    # --- _check_youtube_oauth unit tests ---

    def test_check_youtube_oauth_returns_true_on_success(self):
        """_check_youtube_oauth returns (True, None) when refresh_youtube_token succeeds."""
        from hypogeum.startup import _check_youtube_oauth
        cfg = make_config_with_youtube(all_three=True)
        with patch("hypogeum.publishing.youtube.refresh_youtube_token", return_value="access-token"):
            ok, err = _check_youtube_oauth(cfg)
        assert ok is True
        assert err is None

    def test_check_youtube_oauth_returns_false_on_hard_failure(self):
        """_check_youtube_oauth returns (False, msg) on HardFailureError."""
        from hypogeum.startup import _check_youtube_oauth
        from hypogeum.publishing.retry import HardFailureError
        cfg = make_config_with_youtube(all_three=True)
        with patch(
            "hypogeum.publishing.youtube.refresh_youtube_token",
            side_effect=HardFailureError("HTTP 401", status_code=401),
        ):
            ok, err = _check_youtube_oauth(cfg)
        assert ok is False
        assert err is not None
        assert len(err) > 0

    def test_check_youtube_oauth_returns_false_on_soft_failure(self):
        """_check_youtube_oauth returns (False, msg) on SoftFailureError."""
        from hypogeum.startup import _check_youtube_oauth
        from hypogeum.publishing.retry import SoftFailureError
        cfg = make_config_with_youtube(all_three=True)
        with patch(
            "hypogeum.publishing.youtube.refresh_youtube_token",
            side_effect=SoftFailureError("network timeout"),
        ):
            ok, err = _check_youtube_oauth(cfg)
        assert ok is False
        assert err is not None

    def test_check_youtube_oauth_does_not_raise(self):
        """_check_youtube_oauth never propagates exceptions — always returns a tuple."""
        from hypogeum.startup import _check_youtube_oauth
        cfg = make_config_with_youtube(all_three=True)
        with patch(
            "hypogeum.publishing.youtube.refresh_youtube_token",
            side_effect=RuntimeError("unexpected"),
        ):
            ok, err = _check_youtube_oauth(cfg)
        assert ok is False
        assert err is not None
