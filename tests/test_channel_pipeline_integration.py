"""
tests/test_channel_pipeline_integration.py — P1-02: Channel → Pipeline integration tests

Coverage:
    TestApplyChannelConfig       — Config.apply_channel_config() maps model/voice fields
    TestChannelNotFound          — ChannelNotFoundError raised before any agent runs
    TestChannelNotActive         — ChannelNotActiveError raised when channel not active
    TestChannelIdInRunRegistry   — register_run() writes channel_id to DB and JSON
    TestRunPipelineChannelId     — run_pipeline() wires channel_id end-to-end
    TestCLIChannelIdArg          — run_pipeline.py CLI accepts --channel-id
    TestBackwardCompatNoChannel  — omitting channel_id leaves existing behaviour intact
    TestNoNetworkCalls           — no sockets opened during channel loading
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.channel_config import (
    BrandProfile,
    ChannelConfig,
    ChannelProfile,
    ModelProfile,
    PublishingProfile,
    bootstrap_channel,
)
from hypogeum.config import Config
from hypogeum.db import get_connection, initialize_schema
from hypogeum.pipeline import (
    ChannelNotActiveError,
    ChannelNotFoundError,
    PipelineResult,
    run_pipeline,
)
from hypogeum.run_registry import register_run
from hypogeum.startup import StartupReport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "test_integration.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def minimal_config():
    """Config with enough fields to instantiate without a real .env."""
    original = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")}
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    try:
        yield Config()
    finally:
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


@pytest.fixture()
def active_channel(db_path):
    """Bootstrap an 'active' hypogeum channel into a test DB."""
    bootstrap_channel(
        {
            "channel_id": "hypogeum",
            "name": "The Hypogeum Chronicles",
            "brand": {"language": "en"},
            "publishing": {"enabled_platforms": ["youtube"]},
            "models": {
                "episode_producer":   "anthropic/claude-fable-5",
                "research_director":  "anthropic/claude-opus-4.6",
                "thumbnail_planner":  "openai/gpt-5.5-pro",
                "brand_guardian_p1":  "anthropic/claude-sonnet-4.6",
                "brand_guardian_p2":  "openai/gpt-5.5-pro",
                "narration_voice_id": "j9jfwdrw7BRfcR43Qohk",
                "image_provider":     "muapi",
                "video_provider":     "muapi",
            },
        },
        db_path=db_path,
    )
    return db_path


@pytest.fixture()
def paused_channel(db_path):
    """Bootstrap a 'paused' channel into a test DB."""
    from hypogeum.channel_config import create_channel_profile
    # Create directly with paused status (bypassing bootstrap which sets 'active')
    bootstrap_channel(
        {
            "channel_id": "paused_ch",
            "name": "Paused Channel",
            "brand": {"language": "en"},
            "publishing": {"enabled_platforms": ["youtube"]},
            "models": {
                "episode_producer": "anthropic/claude-sonnet-4.6",
            },
        },
        db_path=db_path,
    )
    # Force the status to 'paused'
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE channel_profiles SET status = 'paused' WHERE channel_id = 'paused_ch'"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _make_channel_config(
    ep_model="anthropic/claude-fable-5",
    rd_model="anthropic/claude-opus-4.6",
    td_model="openai/gpt-5.5-pro",
    bga1="anthropic/claude-sonnet-4.6",
    bga2="openai/gpt-5.5-pro",
    voice_id="j9jfwdrw7BRfcR43Qohk",
    status="active",
) -> ChannelConfig:
    """Build a minimal ChannelConfig for unit tests (no DB needed)."""
    channel = ChannelProfile(
        channel_id="hypogeum",
        name="The Hypogeum Chronicles",
        status=status,
    )
    models = ModelProfile(
        model_profile_id="mp-test",
        name="Test Models",
        episode_producer=ep_model,
        research_director=rd_model,
        thumbnail_planner=td_model,
        brand_guardian_p1=bga1,
        brand_guardian_p2=bga2,
        narration_voice_id=voice_id,
    )
    return ChannelConfig(channel=channel, models=models)


def _make_startup_report(status="healthy") -> StartupReport:
    return StartupReport(
        overall_status=status,
        openrouter_reachable=True,
        models_verified=True,
        fallback_active=False,
        fallback_configured=True,
        checked_at="2026-06-15T00:00:00Z",
    )


# ===========================================================================
# TestApplyChannelConfig
# ===========================================================================

class TestApplyChannelConfig:
    """Config.apply_channel_config() maps ModelProfile fields onto Config."""

    def test_ep_model_overridden(self, minimal_config):
        cc = _make_channel_config(ep_model="anthropic/claude-fable-5")
        minimal_config.EP_MODEL = "old-ep-model"
        minimal_config.apply_channel_config(cc)
        assert minimal_config.EP_MODEL == "anthropic/claude-fable-5"

    def test_rd_model_overridden(self, minimal_config):
        cc = _make_channel_config(rd_model="anthropic/claude-opus-4.6")
        minimal_config.RD_MODEL = "old-rd-model"
        minimal_config.apply_channel_config(cc)
        assert minimal_config.RD_MODEL == "anthropic/claude-opus-4.6"

    def test_td_model_overridden(self, minimal_config):
        cc = _make_channel_config(td_model="openai/gpt-5.5-pro")
        minimal_config.TD_PLANNING_MODEL = "old-td-model"
        minimal_config.apply_channel_config(cc)
        assert minimal_config.TD_PLANNING_MODEL == "openai/gpt-5.5-pro"

    def test_bga1_model_overridden(self, minimal_config):
        cc = _make_channel_config(bga1="anthropic/claude-sonnet-4.6")
        minimal_config.BGA_PASS1_MODEL = "old-bga1"
        minimal_config.apply_channel_config(cc)
        assert minimal_config.BGA_PASS1_MODEL == "anthropic/claude-sonnet-4.6"

    def test_bga2_model_overridden(self, minimal_config):
        cc = _make_channel_config(bga2="openai/gpt-5.5-pro")
        minimal_config.BGA_PASS2_MODEL = "old-bga2"
        minimal_config.apply_channel_config(cc)
        assert minimal_config.BGA_PASS2_MODEL == "openai/gpt-5.5-pro"

    def test_voice_id_overridden(self, minimal_config):
        cc = _make_channel_config(voice_id="j9jfwdrw7BRfcR43Qohk")
        minimal_config.ELEVENLABS_VOICE_ID = "old-voice-id"
        minimal_config.apply_channel_config(cc)
        assert minimal_config.ELEVENLABS_VOICE_ID == "j9jfwdrw7BRfcR43Qohk"

    def test_none_voice_id_does_not_override(self, minimal_config):
        """A None voice_id in the channel profile must not erase the .env value."""
        cc = _make_channel_config(voice_id=None)
        original_voice = "keep-this-voice-id"
        minimal_config.ELEVENLABS_VOICE_ID = original_voice
        # Rebuild with None voice
        cc.models.narration_voice_id = None
        minimal_config.apply_channel_config(cc)
        assert minimal_config.ELEVENLABS_VOICE_ID == original_voice

    def test_all_five_model_fields_applied_in_one_call(self, minimal_config):
        cc = _make_channel_config(
            ep_model="ep-slug",
            rd_model="rd-slug",
            td_model="td-slug",
            bga1="bga1-slug",
            bga2="bga2-slug",
        )
        minimal_config.apply_channel_config(cc)
        assert minimal_config.EP_MODEL == "ep-slug"
        assert minimal_config.RD_MODEL == "rd-slug"
        assert minimal_config.TD_PLANNING_MODEL == "td-slug"
        assert minimal_config.BGA_PASS1_MODEL == "bga1-slug"
        assert minimal_config.BGA_PASS2_MODEL == "bga2-slug"

    def test_apply_returns_self_for_chaining(self, minimal_config):
        cc = _make_channel_config()
        result = minimal_config.apply_channel_config(cc)
        assert result is minimal_config

    def test_no_models_profile_is_no_op(self, minimal_config):
        """ChannelConfig with models=None must leave Config unchanged."""
        cc = ChannelConfig(
            channel=ChannelProfile(channel_id="test", name="test", status="active"),
            models=None,
        )
        minimal_config.EP_MODEL = "untouched"
        minimal_config.apply_channel_config(cc)
        assert minimal_config.EP_MODEL == "untouched"


# ===========================================================================
# TestChannelNotFound
# ===========================================================================

class TestChannelNotFound:
    """ChannelNotFoundError raised before any agent or registration."""

    def test_raises_when_channel_absent(self, minimal_config, db_path):
        with patch("hypogeum.channel_config.load_channel_config", return_value=None) as mock_load:
            with pytest.raises(ChannelNotFoundError) as exc_info:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_test",
                    config=minimal_config,
                    channel_id="missing_channel",
                )
        assert exc_info.value.channel_id == "missing_channel"

    def test_error_message_includes_channel_id(self, minimal_config):
        with patch("hypogeum.channel_config.load_channel_config", return_value=None):
            with pytest.raises(ChannelNotFoundError) as exc_info:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_test",
                    config=minimal_config,
                    channel_id="no_such_channel",
                )
        assert "no_such_channel" in str(exc_info.value)

    def test_error_is_pipeline_error_subclass(self):
        from hypogeum.pipeline import PipelineError
        err = ChannelNotFoundError("ch_test")
        assert isinstance(err, PipelineError)

    def test_no_agent_called_when_channel_missing(self, minimal_config):
        with patch("hypogeum.channel_config.load_channel_config", return_value=None):
            with patch("hypogeum.pipeline._run_executive_producer") as mock_ep:
                with pytest.raises(ChannelNotFoundError):
                    run_pipeline(
                        topic_input="Test",
                        episode_id="ep_test",
                        config=minimal_config,
                        channel_id="ghost",
                    )
        mock_ep.assert_not_called()

    def test_no_run_registered_when_channel_missing(self, minimal_config, tmp_path):
        runs_dir = tmp_path / "runs"
        with patch("hypogeum.channel_config.load_channel_config", return_value=None):
            with pytest.raises(ChannelNotFoundError):
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_test",
                    config=minimal_config,
                    channel_id="ghost",
                )
        assert not runs_dir.exists() or not list(runs_dir.glob("*.json"))


# ===========================================================================
# TestChannelNotActive
# ===========================================================================

class TestChannelNotActive:
    """ChannelNotActiveError raised when channel status != 'active'."""

    def test_raises_on_paused_channel(self, minimal_config, paused_channel):
        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(status="paused"),
        ):
            with pytest.raises(ChannelNotActiveError) as exc_info:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_test",
                    config=minimal_config,
                    channel_id="paused_ch",
                )
        assert exc_info.value.channel_id == "paused_ch"
        assert exc_info.value.status == "paused"

    def test_raises_on_configured_channel(self, minimal_config):
        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(status="configured"),
        ):
            with pytest.raises(ChannelNotActiveError) as exc_info:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_test",
                    config=minimal_config,
                    channel_id="configured_ch",
                )
        assert exc_info.value.status == "configured"

    def test_error_message_includes_status(self, minimal_config):
        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(status="blocked"),
        ):
            with pytest.raises(ChannelNotActiveError) as exc_info:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_test",
                    config=minimal_config,
                    channel_id="blocked_ch",
                )
        assert "blocked" in str(exc_info.value)

    def test_no_agent_called_when_channel_not_active(self, minimal_config):
        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(status="paused"),
        ):
            with patch("hypogeum.pipeline._run_executive_producer") as mock_ep:
                with pytest.raises(ChannelNotActiveError):
                    run_pipeline(
                        topic_input="Test",
                        episode_id="ep_test",
                        config=minimal_config,
                        channel_id="paused_ch",
                    )
        mock_ep.assert_not_called()

    def test_active_channel_does_not_raise(self, minimal_config, active_channel, tmp_path):
        """An active channel must not raise ChannelNotActiveError."""
        report = _make_startup_report()
        with patch("hypogeum.channel_config.load_channel_config",
                   return_value=_make_channel_config(status="active")):
            with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
                with patch("hypogeum.pipeline._run_executive_producer",
                           side_effect=NotImplementedError("stub")):
                    with pytest.raises(NotImplementedError):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_test",
                            config=minimal_config,
                            channel_id="hypogeum",
                            output_dir=tmp_path,
                        )


# ===========================================================================
# TestChannelIdInRunRegistry
# ===========================================================================

class TestChannelIdInRunRegistry:
    """register_run() writes channel_id to the DB column and JSON record."""

    def test_channel_id_written_to_db(self, minimal_config, db_path, tmp_path):
        run_id = register_run(
            episode_id="ep_test",
            config=minimal_config,
            db_path=db_path,
            runs_dir=tmp_path / "runs",
            channel_id="hypogeum",
        )
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT channel_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["channel_id"] == "hypogeum"

    def test_channel_id_written_to_json(self, minimal_config, db_path, tmp_path):
        runs_dir = tmp_path / "runs"
        run_id = register_run(
            episode_id="ep_test",
            config=minimal_config,
            db_path=db_path,
            runs_dir=runs_dir,
            channel_id="hypogeum",
        )
        run_json = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert run_json.get("channel_id") == "hypogeum"

    def test_no_channel_id_leaves_db_column_null(self, minimal_config, db_path, tmp_path):
        run_id = register_run(
            episode_id="ep_test",
            config=minimal_config,
            db_path=db_path,
            runs_dir=tmp_path / "runs",
        )
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT channel_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["channel_id"] is None

    def test_no_channel_id_absent_from_json(self, minimal_config, db_path, tmp_path):
        runs_dir = tmp_path / "runs"
        run_id = register_run(
            episode_id="ep_test",
            config=minimal_config,
            db_path=db_path,
            runs_dir=runs_dir,
        )
        run_json = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert "channel_id" not in run_json

    def test_channel_id_column_exists_in_db_schema(self, db_path):
        conn = get_connection(db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        finally:
            conn.close()
        assert "channel_id" in cols


# ===========================================================================
# TestRunPipelineChannelId
# ===========================================================================

class TestRunPipelineChannelId:
    """run_pipeline() end-to-end channel_id wiring."""

    def test_load_channel_config_called_with_channel_id(self, minimal_config, tmp_path):
        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(),
        ) as mock_load:
            with patch("hypogeum.pipeline.run_startup_checks",
                       return_value=_make_startup_report()):
                with patch("hypogeum.pipeline._run_executive_producer",
                           side_effect=NotImplementedError("stub")):
                    with pytest.raises(NotImplementedError):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_test",
                            config=minimal_config,
                            channel_id="hypogeum",
                            output_dir=tmp_path,
                        )
        mock_load.assert_called_once_with("hypogeum")

    def test_channel_config_values_applied_to_config(self, minimal_config, tmp_path):
        minimal_config.EP_MODEL = "old-ep"
        minimal_config.ELEVENLABS_VOICE_ID = "old-voice"

        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(
                ep_model="anthropic/claude-fable-5",
                voice_id="j9jfwdrw7BRfcR43Qohk",
            ),
        ):
            with patch("hypogeum.pipeline.run_startup_checks",
                       return_value=_make_startup_report()):
                with patch("hypogeum.pipeline._run_executive_producer",
                           side_effect=NotImplementedError("stub")):
                    with pytest.raises(NotImplementedError):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_test",
                            config=minimal_config,
                            channel_id="hypogeum",
                            output_dir=tmp_path,
                        )
        assert minimal_config.EP_MODEL == "anthropic/claude-fable-5"
        assert minimal_config.ELEVENLABS_VOICE_ID == "j9jfwdrw7BRfcR43Qohk"

    def test_load_not_called_when_channel_id_none(self, minimal_config, tmp_path):
        with patch(
            "hypogeum.channel_config.load_channel_config",
        ) as mock_load:
            with patch("hypogeum.pipeline.run_startup_checks",
                       return_value=_make_startup_report()):
                with patch("hypogeum.pipeline._run_executive_producer",
                           side_effect=NotImplementedError("stub")):
                    with pytest.raises(NotImplementedError):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_test",
                            config=minimal_config,
                            channel_id=None,
                            output_dir=tmp_path,
                        )
        mock_load.assert_not_called()


# ===========================================================================
# TestCLIChannelIdArg
# ===========================================================================

class TestCLIChannelIdArg:
    """run_pipeline.py CLI accepts --channel-id and forwards to run_pipeline()."""

    def _run_main(self, argv):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "run_pipeline_mod",
            Path(__file__).parent.parent / "run_pipeline.py",
        )
        mod = importlib.util.module_from_spec(spec)
        orig = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        try:
            with patch("sys.argv", ["run_pipeline.py"] + argv):
                spec.loader.exec_module(mod)
                return mod.main()
        finally:
            if orig is not None:
                os.environ["OPENROUTER_API_KEY"] = orig
            else:
                os.environ.pop("OPENROUTER_API_KEY", None)

    def test_channel_id_arg_accepted(self):
        """--channel-id hypogeum should not produce an argparse error."""
        err = ChannelNotFoundError("hypogeum")
        with patch("hypogeum.pipeline.run_pipeline", side_effect=err):
            # Exit code 4 = pipeline error; but no argparse error (exit 2)
            code = self._run_main([
                "--topic", "Test",
                "--episode-id", "ep_cli_test",
                "--channel-id", "hypogeum",
            ])
        assert code == 4  # ChannelNotFoundError → exit 4

    def test_channel_not_found_returns_4(self):
        with patch(
            "hypogeum.pipeline.run_pipeline",
            side_effect=ChannelNotFoundError("no_channel"),
        ):
            code = self._run_main([
                "--topic", "Test",
                "--episode-id", "ep_x",
                "--channel-id", "no_channel",
            ])
        assert code == 4

    def test_channel_not_active_returns_4(self):
        with patch(
            "hypogeum.pipeline.run_pipeline",
            side_effect=ChannelNotActiveError("paused_ch", "paused"),
        ):
            code = self._run_main([
                "--topic", "Test",
                "--episode-id", "ep_x",
                "--channel-id", "paused_ch",
            ])
        assert code == 4

    def test_no_channel_id_omitted_defaults_to_hypogeum(self):
        """Omitting --channel-id passes channel_id='hypogeum' to run_pipeline()."""
        captured = {}

        def capture(**kwargs):
            captured["channel_id"] = kwargs.get("channel_id")
            raise NotImplementedError("stop here")

        with patch("hypogeum.pipeline.run_pipeline", side_effect=capture):
            self._run_main(["--topic", "Test", "--episode-id", "ep_x"])

        assert captured.get("channel_id") == "hypogeum"

    def test_channel_id_forwarded_to_run_pipeline(self):
        """--channel-id value must reach run_pipeline() as channel_id kwarg."""
        captured = {}

        def capture(**kwargs):
            captured["channel_id"] = kwargs.get("channel_id")
            raise NotImplementedError("stop here")

        with patch("hypogeum.pipeline.run_pipeline", side_effect=capture):
            self._run_main([
                "--topic", "Test",
                "--episode-id", "ep_x",
                "--channel-id", "hypogeum",
            ])

        assert captured.get("channel_id") == "hypogeum"


# ===========================================================================
# TestBackwardCompatNoChannel
# ===========================================================================

class TestBackwardCompatNoChannel:
    """Omitting channel_id leaves the existing pipeline behaviour unchanged."""

    def test_pipeline_runs_without_channel_id(self, minimal_config, tmp_path):
        """run_pipeline() with explicit channel_id=None must not load a channel."""
        with patch("hypogeum.channel_config.load_channel_config") as mock_load:
            with patch("hypogeum.pipeline.run_startup_checks",
                       return_value=_make_startup_report()):
                with patch("hypogeum.pipeline._run_executive_producer",
                           side_effect=NotImplementedError("stub")):
                    with pytest.raises(NotImplementedError):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_compat",
                            config=minimal_config,
                            output_dir=tmp_path,
                            channel_id=None,  # explicit opt-out
                        )
        mock_load.assert_not_called()

    def test_env_model_values_unchanged_without_channel(self, minimal_config, tmp_path):
        """With explicit channel_id=None, Config values must come from .env only."""
        original_ep = minimal_config.EP_MODEL
        with patch("hypogeum.pipeline.run_startup_checks",
                   return_value=_make_startup_report()):
            with patch("hypogeum.pipeline._run_executive_producer",
                       side_effect=NotImplementedError("stub")):
                with pytest.raises(NotImplementedError):
                    run_pipeline(
                        topic_input="Test",
                        episode_id="ep_compat",
                        config=minimal_config,
                        output_dir=tmp_path,
                        channel_id=None,  # explicit opt-out
                    )
        assert minimal_config.EP_MODEL == original_ep


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    def test_channel_config_load_opens_no_sockets(self, active_channel):
        """load_channel_config() must read from DB only — no network."""
        from hypogeum.channel_config import load_channel_config

        original_socket = socket.socket

        def fail_on_connect(*args, **kwargs):
            raise AssertionError("No network calls allowed during channel loading")

        with patch("socket.socket", side_effect=fail_on_connect):
            # Should not raise (no network call made)
            result = load_channel_config("hypogeum", db_path=active_channel)
        assert result is not None

    def test_apply_channel_config_opens_no_sockets(self, minimal_config):
        """Config.apply_channel_config() is pure in-memory — no network."""
        cc = _make_channel_config()
        with patch("socket.socket", side_effect=AssertionError("no network")):
            minimal_config.apply_channel_config(cc)  # must not raise


# ===========================================================================
# TestDefaultChannelId  (P1-02 acceptance)
# ===========================================================================

class TestDefaultChannelId:
    """run_pipeline() defaults channel_id to 'hypogeum' when not supplied."""

    def test_omitting_channel_id_loads_hypogeum(self, minimal_config, tmp_path):
        """Omitting channel_id must call load_channel_config('hypogeum')."""
        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(),
        ) as mock_load:
            with patch("hypogeum.pipeline.run_startup_checks",
                       return_value=_make_startup_report()):
                with patch("hypogeum.pipeline._run_executive_producer",
                           side_effect=NotImplementedError("stub")):
                    with pytest.raises(NotImplementedError):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_default",
                            config=minimal_config,
                            output_dir=tmp_path,
                            # channel_id not passed — must default to "hypogeum"
                        )
        mock_load.assert_called_once_with("hypogeum")

    def test_missing_default_channel_raises_not_found(self, minimal_config, tmp_path):
        """Missing default 'hypogeum' channel raises ChannelNotFoundError before execution."""
        with patch("hypogeum.channel_config.load_channel_config", return_value=None):
            with pytest.raises(ChannelNotFoundError) as exc_info:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_default",
                    config=minimal_config,
                    output_dir=tmp_path,
                )
        assert exc_info.value.channel_id == "hypogeum"

    def test_inactive_default_channel_raises_not_active(self, minimal_config, tmp_path):
        """Inactive default 'hypogeum' channel raises ChannelNotActiveError before execution."""
        with patch(
            "hypogeum.channel_config.load_channel_config",
            return_value=_make_channel_config(status="paused"),
        ):
            with pytest.raises(ChannelNotActiveError) as exc_info:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_default",
                    config=minimal_config,
                    output_dir=tmp_path,
                )
        assert exc_info.value.channel_id == "hypogeum"
        assert exc_info.value.status == "paused"

    def test_no_agent_called_when_default_channel_missing(self, minimal_config, tmp_path):
        """Pipeline must not invoke any agent when default channel is missing."""
        with patch("hypogeum.channel_config.load_channel_config", return_value=None):
            with patch("hypogeum.pipeline._run_executive_producer") as mock_ep:
                with pytest.raises(ChannelNotFoundError):
                    run_pipeline(
                        topic_input="Test",
                        episode_id="ep_default",
                        config=minimal_config,
                        output_dir=tmp_path,
                    )
        mock_ep.assert_not_called()
