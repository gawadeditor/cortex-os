"""
tests/test_worker_singleton_and_heartbeat.py — run_worker.py's 2026-08-23
autonomous-runtime additions: singleton PID lock, heartbeat, and
per-channel operator_enabled pause in discovery-refill/publishing.

Every test patches run_worker._WORKER_PID_FILE to an isolated tmp_path
location -- the real constant is a shared, hardcoded, cross-module path
(also read by run_dashboard.py and hypogeum/observability.py), never
test-isolated on its own; touching it directly here would corrupt state
for any other test or a real worker on the same machine.

Covers:
  TestSingletonLock       — refuses a second concurrent worker; allows a
                             stale (dead-PID) one; --once bypasses the lock
  TestHeartbeat            — worker_heartbeat_at is written every iteration
  TestPerChannelPause      — operator_enabled=False skips discovery-refill
                              and autonomous publishing for that channel
                              only, never affecting another channel
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from hypogeum.channel_config import create_channel_operations_profile, create_channel_profile
from hypogeum.db import get_worker_control, initialize_schema


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    with patch("hypogeum.config.Config", return_value=cfg):
        yield cfg


@pytest.fixture
def isolated_pid_file(tmp_path, monkeypatch):
    """Never touches the real, shared .hypogeum-assets/worker.pid."""
    pid_file = tmp_path / "isolated_worker.pid"
    monkeypatch.setattr("run_worker._WORKER_PID_FILE", pid_file)
    return pid_file


class TestSingletonLock:
    def test_refuses_second_worker_when_pid_is_alive(self, tmp_path, mock_config, isolated_pid_file):
        db = tmp_path / "h.db"
        initialize_schema(db)
        isolated_pid_file.write_text("424242")

        from run_worker import run_worker
        with patch("run_worker._os.kill") as mock_kill:
            mock_kill.return_value = None  # os.kill(pid, 0) succeeds -> process is alive
            exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=False, channel_id=None)

        assert exit_code == 1
        # Refused before ever writing its own PID -- the stale check ran first.
        assert isolated_pid_file.read_text().strip() == "424242"

    def test_allows_start_when_recorded_pid_is_dead(self, tmp_path, mock_config, isolated_pid_file):
        db = tmp_path / "h.db"
        initialize_schema(db)
        isolated_pid_file.write_text("424242")

        from run_worker import run_worker
        with patch("run_worker._os.kill", side_effect=ProcessLookupError):
            exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 0
        # Started successfully despite the stale entry -- overwrote it with
        # its own PID during the run, then cleaned up on its own graceful
        # exit (once=True completes after one iteration), so the stale
        # "424242" value must never still be present either way.
        assert not isolated_pid_file.exists() or isolated_pid_file.read_text().strip() != "424242"

    def test_once_bypasses_the_singleton_lock(self, tmp_path, mock_config, isolated_pid_file):
        """--once is a supervised, bounded, single-shot invocation meant to
        run alongside (or instead of) a long-running daemon -- never the
        process the lock protects against."""
        db = tmp_path / "h.db"
        initialize_schema(db)
        isolated_pid_file.write_text("424242")

        from run_worker import run_worker
        with patch("run_worker._os.kill") as mock_kill:
            mock_kill.return_value = None  # alive, but must not matter for once=True
            exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 0

    def test_clean_exit_removes_its_own_pid_file(self, tmp_path, mock_config, isolated_pid_file):
        db = tmp_path / "h.db"
        initialize_schema(db)
        assert not isolated_pid_file.exists()

        from run_worker import run_worker
        exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 0
        assert not isolated_pid_file.exists()


class TestHeartbeat:
    def test_heartbeat_written_on_iteration(self, tmp_path, mock_config, isolated_pid_file):
        db = tmp_path / "h.db"
        initialize_schema(db)

        from run_worker import run_worker
        before = get_worker_control("worker_heartbeat_at", default="", db_path=db)
        assert before == ""

        exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 0
        after = get_worker_control("worker_heartbeat_at", default="", db_path=db)
        assert after != ""


class TestPerChannelPause:
    def test_operator_disabled_channel_skipped_by_discovery_refill(self, tmp_path):
        db = tmp_path / "h.db"
        initialize_schema(db)
        create_channel_profile(channel_id="ch_off", name="Off", db_path=db)
        create_channel_operations_profile(
            channel_id="ch_off", channel_name="Off", timezone="UTC",
            operator_enabled=False, db_path=db,
        )

        import run_worker
        with patch("hypogeum.channel_config.list_channel_profiles") as mock_list, \
             patch("hypogeum.topic_discovery.run_discovery_cycle") as mock_discovery:
            mock_ch = MagicMock()
            mock_ch.channel_id = "ch_off"
            mock_list.return_value = [mock_ch]

            result = run_worker._check_and_refill_queue(
                db_path=db, threshold=100, auto_approve=False,
            )

        mock_discovery.assert_not_called()
        assert result["channels_checked"] == 1

    def test_operator_enabled_channel_unaffected_by_a_disabled_sibling(self, tmp_path):
        db = tmp_path / "h.db"
        initialize_schema(db)
        create_channel_profile(channel_id="ch_off", name="Off", db_path=db)
        create_channel_operations_profile(
            channel_id="ch_off", channel_name="Off", timezone="UTC",
            operator_enabled=False, db_path=db,
        )
        create_channel_profile(channel_id="ch_on", name="On", db_path=db)
        create_channel_operations_profile(
            channel_id="ch_on", channel_name="On", timezone="UTC",
            operator_enabled=True, db_path=db,
        )

        import run_worker
        with patch("hypogeum.channel_config.list_channel_profiles") as mock_list, \
             patch("hypogeum.topic_discovery.run_discovery_cycle") as mock_discovery:
            ch_off, ch_on = MagicMock(), MagicMock()
            ch_off.channel_id, ch_on.channel_id = "ch_off", "ch_on"
            mock_list.return_value = [ch_off, ch_on]
            mock_discovery.return_value = {"ok": True, "cycle_id": "c1", "candidate_count": 0, "selected_candidate_id": None}

            run_worker._check_and_refill_queue(db_path=db, threshold=100, auto_approve=False)

        called_channels = [c.args[0] for c in mock_discovery.call_args_list]
        assert "ch_off" not in called_channels
        assert "ch_on" in called_channels

    def test_channel_with_no_operations_profile_is_unaffected_fail_open(self, tmp_path):
        """A channel that never configured a ChannelOperationsProfile at
        all behaves exactly as before this gate existed -- fail-open on
        absence, matching every other optional-profile gate in this loop."""
        db = tmp_path / "h.db"
        initialize_schema(db)

        import run_worker
        with patch("hypogeum.channel_config.list_channel_profiles") as mock_list, \
             patch("hypogeum.topic_discovery.run_discovery_cycle") as mock_discovery:
            mock_ch = MagicMock()
            mock_ch.channel_id = "ch_no_profile"
            mock_list.return_value = [mock_ch]
            mock_discovery.return_value = {"ok": True, "cycle_id": "c1", "candidate_count": 0, "selected_candidate_id": None}

            run_worker._check_and_refill_queue(db_path=db, threshold=100, auto_approve=False)

        mock_discovery.assert_called_once_with("ch_no_profile", db_path=db)
