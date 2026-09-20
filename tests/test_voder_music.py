"""
tests/test_voder_music.py — Unit tests for hypogeum/voder_music.py

VODER's own subprocess/CLI is never invoked for real. No models, no real
audio generation. Duration probing is mocked (hypogeum.elevenlabs's
ffprobe-backed _probe_audio_duration_seconds).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.config import Config
from hypogeum.voder_music import (
    VoderMusicError,
    VoderNotConfiguredError,
    generate_music_via_voder,
)


def _make_config(voder_home: str | None, timeout: int = 60) -> Config:
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-or-key"}, clear=True):
        cfg = Config()
    cfg.VODER_HOME = voder_home
    cfg.VODER_TTM_TIMEOUT_SECONDS = timeout
    return cfg


def _make_fake_voder_checkout(tmp_path: Path) -> Path:
    """A VODER checkout is just: <home>/src/voder.py + <home>/.venv/bin/python3."""
    home = tmp_path / "voder_home"
    (home / "src").mkdir(parents=True)
    (home / "src" / "voder.py").write_text("# stub")
    venv_bin = home / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").write_text("#!/bin/sh\n")
    (venv_bin / "python3").chmod(0o755)
    return home


class TestNotConfigured:
    def test_no_voder_home_raises(self, tmp_path):
        cfg = _make_config(voder_home=None)
        with pytest.raises(VoderNotConfiguredError):
            generate_music_via_voder(
                style_prompt="gentle lullaby", required_duration_seconds=20.0,
                output_path=tmp_path / "out.wav", config=cfg,
            )

    def test_nonexistent_checkout_raises(self, tmp_path):
        cfg = _make_config(voder_home=str(tmp_path / "does_not_exist"))
        with pytest.raises(VoderNotConfiguredError):
            generate_music_via_voder(
                style_prompt="gentle lullaby", required_duration_seconds=20.0,
                output_path=tmp_path / "out.wav", config=cfg,
            )

    def test_checkout_without_venv_raises(self, tmp_path):
        home = tmp_path / "voder_home"
        (home / "src").mkdir(parents=True)
        (home / "src" / "voder.py").write_text("# stub")
        cfg = _make_config(voder_home=str(home))
        with pytest.raises(VoderNotConfiguredError):
            generate_music_via_voder(
                style_prompt="gentle lullaby", required_duration_seconds=20.0,
                output_path=tmp_path / "out.wav", config=cfg,
            )


class TestSuccessfulGeneration:
    def _run(self, tmp_path, duration=20.0, cli_reported_path=None):
        home = _make_fake_voder_checkout(tmp_path)
        cfg = _make_config(voder_home=str(home))
        generated = home / "results" / "voder_ttm_fake.wav"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"\x00" * 16)
        output_path = tmp_path / "final" / "track.wav"

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.returncode = 0
        fake_result.stdout = f"✓ Success! Output saved to: {cli_reported_path or generated}\n"
        fake_result.stderr = ""

        with patch("hypogeum.voder_music.subprocess.run", return_value=fake_result) as mock_run, \
             patch("hypogeum.elevenlabs._probe_audio_duration_seconds", return_value=duration):
            actual = generate_music_via_voder(
                style_prompt="gentle lullaby, soft piano", required_duration_seconds=duration,
                output_path=output_path, config=cfg,
            )
        return actual, output_path, mock_run

    def test_returns_probed_duration(self, tmp_path):
        actual, output_path, _ = self._run(tmp_path, duration=20.0)
        assert actual == 20.0

    def test_copies_file_to_output_path(self, tmp_path):
        _, output_path, _ = self._run(tmp_path)
        assert output_path.exists()
        assert output_path.read_bytes() == b"\x00" * 16

    def test_command_includes_instrumental_lyrics_and_styling(self, tmp_path):
        _, _, mock_run = self._run(tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "ttm" in cmd
        assert "lyrics" in cmd
        assert "..." in cmd  # instrumental marker
        assert "styling" in cmd
        assert "gentle lullaby, soft piano" in cmd

    def test_duration_clamped_to_300_cap(self, tmp_path):
        _, _, mock_run = self._run(tmp_path, duration=500.0)
        cmd = mock_run.call_args[0][0]
        assert "300" in cmd

    def test_duration_clamped_to_10_floor(self, tmp_path):
        _, _, mock_run = self._run(tmp_path, duration=3.0)
        cmd = mock_run.call_args[0][0]
        assert "10" in cmd


class TestFailureModes:
    def test_nonzero_exit_raises(self, tmp_path):
        home = _make_fake_voder_checkout(tmp_path)
        cfg = _make_config(voder_home=str(home))
        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "Error: something broke"
        with patch("hypogeum.voder_music.subprocess.run", return_value=fake_result):
            with pytest.raises(VoderMusicError):
                generate_music_via_voder(
                    style_prompt="x", required_duration_seconds=20.0,
                    output_path=tmp_path / "out.wav", config=cfg,
                )

    def test_timeout_raises(self, tmp_path):
        home = _make_fake_voder_checkout(tmp_path)
        cfg = _make_config(voder_home=str(home), timeout=5)
        with patch("hypogeum.voder_music.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="ttm", timeout=5)):
            with pytest.raises(VoderMusicError):
                generate_music_via_voder(
                    style_prompt="x", required_duration_seconds=20.0,
                    output_path=tmp_path / "out.wav", config=cfg,
                )

    def test_no_success_line_raises(self, tmp_path):
        home = _make_fake_voder_checkout(tmp_path)
        cfg = _make_config(voder_home=str(home))
        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.returncode = 0
        fake_result.stdout = "some unrelated output"
        fake_result.stderr = ""
        with patch("hypogeum.voder_music.subprocess.run", return_value=fake_result):
            with pytest.raises(VoderMusicError):
                generate_music_via_voder(
                    style_prompt="x", required_duration_seconds=20.0,
                    output_path=tmp_path / "out.wav", config=cfg,
                )

    def test_reported_path_missing_raises(self, tmp_path):
        home = _make_fake_voder_checkout(tmp_path)
        cfg = _make_config(voder_home=str(home))
        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.returncode = 0
        fake_result.stdout = f"✓ Success! Output saved to: {tmp_path / 'never_written.wav'}\n"
        fake_result.stderr = ""
        with patch("hypogeum.voder_music.subprocess.run", return_value=fake_result):
            with pytest.raises(VoderMusicError):
                generate_music_via_voder(
                    style_prompt="x", required_duration_seconds=20.0,
                    output_path=tmp_path / "out.wav", config=cfg,
                )

    def test_unreadable_output_raises(self, tmp_path):
        home = _make_fake_voder_checkout(tmp_path)
        cfg = _make_config(voder_home=str(home))
        generated = home / "results" / "voder_ttm_fake.wav"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"not really audio")
        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.returncode = 0
        fake_result.stdout = f"✓ Success! Output saved to: {generated}\n"
        fake_result.stderr = ""
        with patch("hypogeum.voder_music.subprocess.run", return_value=fake_result), \
             patch("hypogeum.elevenlabs._probe_audio_duration_seconds", return_value=None):
            with pytest.raises(VoderMusicError):
                generate_music_via_voder(
                    style_prompt="x", required_duration_seconds=20.0,
                    output_path=tmp_path / "out.wav", config=cfg,
                )
