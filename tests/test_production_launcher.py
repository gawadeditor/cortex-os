"""
tests/test_production_launcher.py — F14: production_launcher unit tests
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "launcher_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def full_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
    monkeypatch.setenv("MUAPI_API_KEY", "muapi-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-test")


def _active_channel():
    ch = MagicMock()
    ch.status = "active"
    ch.channel_id = "ch_test"
    return ch


def _inactive_channel():
    ch = MagicMock()
    ch.status = "paused"
    ch.channel_id = "ch_test"
    return ch


class TestBlockedMissingEnvVars:

    def test_blocked_when_openrouter_missing(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("PERPLEXITY_API_KEY", "x")
        monkeypatch.setenv("MUAPI_API_KEY", "x")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
        monkeypatch.setenv("ELEVENLABS_VOICE_ID", "x")
        from hypogeum.production_launcher import launch_episode_from_dashboard
        result = launch_episode_from_dashboard(
            "ch_1", "topic", "long_video", "balanced", [], db_path=db
        )
        assert result["status"] == "blocked"
        assert any("OPENROUTER_API_KEY" in m for m in result["missing"])

    def test_blocked_when_all_env_vars_missing(self, db, monkeypatch):
        for var in ("OPENROUTER_API_KEY", "PERPLEXITY_API_KEY", "MUAPI_API_KEY",
                    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
            monkeypatch.delenv(var, raising=False)
        from hypogeum.production_launcher import launch_episode_from_dashboard
        result = launch_episode_from_dashboard(
            "ch_1", "topic", "long_video", "balanced", [], db_path=db
        )
        assert result["status"] == "blocked"
        assert len(result["missing"]) >= 5

    def test_missing_list_contains_env_prefix(self, db, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "x")
        monkeypatch.setenv("MUAPI_API_KEY", "x")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
        monkeypatch.setenv("ELEVENLABS_VOICE_ID", "x")
        from hypogeum.production_launcher import launch_episode_from_dashboard
        result = launch_episode_from_dashboard(
            "ch_1", "topic", "long_video", "balanced", [], db_path=db
        )
        assert result["status"] == "blocked"
        assert any("env:" in m for m in result["missing"])


class TestBlockedFfmpeg:

    def test_blocked_when_ffmpeg_missing(self, db, full_env):
        with patch("shutil.which", return_value=None):
            from hypogeum.production_launcher import launch_episode_from_dashboard
            result = launch_episode_from_dashboard(
                "ch_test", "topic", "long_video", "balanced", ["youtube"],
                db_path=db,
            )
        assert result["status"] == "blocked"
        assert any("ffmpeg" in m for m in result["missing"])


class TestBlockedChannel:

    def test_blocked_when_channel_not_found(self, db, full_env):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=None):
            from hypogeum.production_launcher import launch_episode_from_dashboard
            result = launch_episode_from_dashboard(
                "ch_missing", "topic", "long_video", "balanced", [],
                db_path=db,
            )
        assert result["status"] == "blocked"
        assert any("not found" in m for m in result["missing"])

    def test_blocked_when_channel_inactive(self, db, full_env):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_inactive_channel()):
            from hypogeum.production_launcher import launch_episode_from_dashboard
            result = launch_episode_from_dashboard(
                "ch_test", "topic", "long_video", "balanced", [],
                db_path=db,
            )
        assert result["status"] == "blocked"
        assert any("not active" in m for m in result["missing"])


class TestSuccessfulLaunch:

    def _launch(self, db, extra_patches=None):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        patches = [
            patch("shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()),
            patch("subprocess.Popen", return_value=mock_proc),
        ]
        ctx = [p.__enter__() for p in patches]
        mock_popen = ctx[2]
        from hypogeum.production_launcher import launch_episode_from_dashboard
        result = launch_episode_from_dashboard(
            "ch_test", "ancient rome", "long_video", "balanced", ["youtube"],
            db_path=db,
        )
        for p in patches:
            p.__exit__(None, None, None)
        return result, mock_popen

    def test_success_calls_popen(self, db, full_env):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            from hypogeum.production_launcher import launch_episode_from_dashboard
            result = launch_episode_from_dashboard(
                "ch_test", "my topic", "long_video", "balanced", ["youtube"],
                db_path=db,
            )
        assert result["status"] == "launched"
        mock_popen.assert_called_once()

    def test_popen_command_contains_run_pipeline(self, db, full_env):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            from hypogeum.production_launcher import launch_episode_from_dashboard
            launch_episode_from_dashboard(
                "ch_test", "my topic", "long_video", "balanced", ["youtube"],
                db_path=db,
            )
        cmd = mock_popen.call_args[0][0]
        assert any("run_pipeline.py" in str(part) for part in cmd)

    def test_popen_command_contains_topic(self, db, full_env):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            from hypogeum.production_launcher import launch_episode_from_dashboard
            launch_episode_from_dashboard(
                "ch_test", "ancient rome", "long_video", "balanced", [],
                db_path=db,
            )
        cmd = mock_popen.call_args[0][0]
        assert "ancient rome" in cmd

    def test_popen_command_contains_episode_id(self, db, full_env):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            from hypogeum.production_launcher import launch_episode_from_dashboard
            result = launch_episode_from_dashboard(
                "ch_test", "my topic", "long_video", "balanced", [],
                db_path=db,
            )
        cmd = mock_popen.call_args[0][0]
        assert result["episode_id"] in cmd

    def test_popen_command_contains_channel_id(self, db, full_env):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            from hypogeum.production_launcher import launch_episode_from_dashboard
            launch_episode_from_dashboard(
                "ch_test", "my topic", "long_video", "balanced", [],
                db_path=db,
            )
        cmd = mock_popen.call_args[0][0]
        assert "ch_test" in cmd

    def test_result_contains_episode_id(self, db, full_env):
        mock_proc = MagicMock()
        mock_proc.pid = 9
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()), \
             patch("subprocess.Popen", return_value=mock_proc):
            from hypogeum.production_launcher import launch_episode_from_dashboard
            result = launch_episode_from_dashboard(
                "ch_test", "my topic", "long_video", "balanced", [],
                db_path=db,
            )
        assert "episode_id" in result
        assert result["episode_id"].startswith("ep_")

    def test_no_mock_analytics_created(self, db, full_env):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("hypogeum.analytics_store.seed_mock_analytics") as mock_seed:
            from hypogeum.production_launcher import launch_episode_from_dashboard
            launch_episode_from_dashboard(
                "ch_test", "my topic", "long_video", "balanced", [],
                db_path=db,
            )
        mock_seed.assert_not_called()
