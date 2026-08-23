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

    def test_once_is_also_covered_by_the_singleton_lock(self, tmp_path, mock_config, isolated_pid_file):
        """
        2026-08-23, corrected same day: --once is exactly as mutation-
        capable as the long-running daemon (claims from run_queue, runs
        the real pipeline) and must NOT be exempt from the lock -- a
        gated or one-shot mutation process must not run concurrently
        with the daemon. This test previously asserted the opposite
        (bypass) before that correction; see
        _acquire_worker_singleton_lock()'s own docstring for the full
        rationale.
        """
        db = tmp_path / "h.db"
        initialize_schema(db)
        isolated_pid_file.write_text("424242")

        from run_worker import run_worker
        with patch("run_worker._os.kill") as mock_kill:
            mock_kill.return_value = None  # a real, live conflicting process
            exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 1

    def test_clean_exit_removes_its_own_pid_file(self, tmp_path, mock_config, isolated_pid_file):
        db = tmp_path / "h.db"
        initialize_schema(db)
        assert not isolated_pid_file.exists()

        from run_worker import run_worker
        exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 0
        assert not isolated_pid_file.exists()


class TestGatedCliSharesTheSingletonLock:
    """
    2026-08-23 (Phase 4 correction): a gated/one-shot mutation process
    (--gated-episode-id) is exactly as mutation-capable as the daemon and
    must share the SAME lock -- a gated invocation must refuse to start
    while the daemon (or another gated invocation) holds it, and the
    daemon must refuse to start while a gated invocation holds it. Two
    distinct entry points, one shared lock, proven from both directions.
    """

    def test_gated_cli_refuses_when_a_real_conflicting_pid_exists(self, tmp_path, isolated_pid_file):
        import run_worker

        db = tmp_path / "h.db"
        initialize_schema(db)
        isolated_pid_file.write_text("424242")

        with patch("run_worker._os.kill") as mock_kill:
            mock_kill.return_value = None  # a real, live conflicting process
            exit_code = run_worker._run_gated_cli(
                channel_id="hypogeum", episode_id="ep_test", topic="A topic", db_path=db,
            )

        assert exit_code == 1
        assert isolated_pid_file.read_text().strip() == "424242"  # never overwritten

    def test_gated_cli_acquires_and_releases_cleanly(self, tmp_path, isolated_pid_file):
        import run_worker
        from hypogeum.queue import PollResult

        db = tmp_path / "h.db"
        initialize_schema(db)
        assert not isolated_pid_file.exists()

        with patch("hypogeum.queue.poll", return_value=PollResult()):
            exit_code = run_worker._run_gated_cli(
                channel_id="hypogeum", episode_id="ep_test", topic="A topic", db_path=db,
            )

        assert exit_code == 1  # PollResult() has no matching proc -> "empty" -> non-zero, unrelated to the lock
        assert not isolated_pid_file.exists()  # lock released on the way out regardless of outcome

    def test_daemon_refuses_to_start_while_a_gated_invocation_holds_the_lock(self, tmp_path, isolated_pid_file, mock_config):
        """Simulates a gated invocation's PID still being recorded (as if
        it were mid-flight) and proves the daemon's own entry point
        refuses to start concurrently against the same lock file."""
        import run_worker

        db = tmp_path / "h.db"
        initialize_schema(db)
        isolated_pid_file.write_text("999999")  # stand-in for an in-flight gated PID

        with patch("run_worker._os.kill") as mock_kill:
            mock_kill.return_value = None  # alive
            exit_code = run_worker.run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 1


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

    def test_channel_with_no_operations_profile_fails_closed(self, tmp_path):
        """
        2026-08-23 (autonomy safety semantics correction): a channel that
        never configured a ChannelOperationsProfile at all must NOT be
        treated as autonomously enabled for a paid, mutating action --
        discovery makes real LLM/Perplexity calls and approve_candidate()
        has no ChannelOperationsProfile gate of its own, so this check is
        the only thing standing between an unconfigured channel and
        autonomous spend. Fails closed on absence (superseding this
        test's own prior name/assertion, which validated fail-open-on-
        absence before this correction)."""
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

        mock_discovery.assert_not_called()

    def test_unreadable_profile_fails_closed_for_discovery(self, tmp_path):
        """A profile row that exists but fails to read/parse
        (get_channel_operations_profile() raises RuntimeError -- corrupt
        row, per that function's own documented contract) must be treated
        exactly like a missing one: fail closed, never proceed as if
        unrestricted."""
        db = tmp_path / "h.db"
        initialize_schema(db)

        import run_worker
        with patch("hypogeum.channel_config.list_channel_profiles") as mock_list, \
             patch("hypogeum.channel_config.get_channel_operations_profile", side_effect=RuntimeError("corrupt row")), \
             patch("hypogeum.topic_discovery.run_discovery_cycle") as mock_discovery:
            mock_ch = MagicMock()
            mock_ch.channel_id = "ch_corrupt"
            mock_list.return_value = [mock_ch]

            run_worker._check_and_refill_queue(db_path=db, threshold=100, auto_approve=False)

        mock_discovery.assert_not_called()

    def test_missing_profile_fails_closed_for_publishing(self, tmp_path):
        import run_worker

        with patch("hypogeum.channel_config.list_channel_profiles") as mock_list, \
             patch("hypogeum.publishing.zernio_channel_release_trigger.process_channel_ready_bundles") as mock_publish:
            mock_ch = MagicMock()
            mock_ch.channel_id = "ch_no_profile"
            mock_list.return_value = [mock_ch]

            result = run_worker._process_channel_publishing(
                db_path=tmp_path / "h.db", assets_root=tmp_path,
            )

        mock_publish.assert_not_called()
        assert result["channels_with_activity"] == 0

    def test_unreadable_profile_fails_closed_for_publishing(self, tmp_path):
        import run_worker

        with patch("hypogeum.channel_config.list_channel_profiles") as mock_list, \
             patch("hypogeum.channel_config.get_channel_operations_profile", side_effect=RuntimeError("corrupt row")), \
             patch("hypogeum.publishing.zernio_channel_release_trigger.process_channel_ready_bundles") as mock_publish:
            mock_ch = MagicMock()
            mock_ch.channel_id = "ch_corrupt"
            mock_list.return_value = [mock_ch]

            run_worker._process_channel_publishing(db_path=tmp_path / "h.db", assets_root=tmp_path)

        mock_publish.assert_not_called()


class TestHeartbeatExpiry:
    """
    2026-08-23 (Phase 4 requirement: 'heartbeat expiry'): worker_heartbeat_at
    alone is just a timestamp -- an external monitor needs a way to decide
    "is this worker stalled" from it. is_worker_heartbeat_stale() is that
    one, small, reusable utility, so a dashboard/health-check caller never
    reimplements the same age-vs-threshold comparison independently.
    """

    def test_fresh_heartbeat_is_not_stale(self, tmp_path):
        import run_worker
        from hypogeum.db import set_worker_control
        from datetime import datetime, timezone

        db = tmp_path / "h.db"
        initialize_schema(db)
        set_worker_control("worker_heartbeat_at", datetime.now(timezone.utc).isoformat(), db_path=db)

        assert run_worker.is_worker_heartbeat_stale(db_path=db, max_age_seconds=120) is False

    def test_old_heartbeat_is_stale(self, tmp_path):
        import run_worker
        from hypogeum.db import set_worker_control
        from datetime import datetime, timezone, timedelta

        db = tmp_path / "h.db"
        initialize_schema(db)
        old = datetime.now(timezone.utc) - timedelta(seconds=600)
        set_worker_control("worker_heartbeat_at", old.isoformat(), db_path=db)

        assert run_worker.is_worker_heartbeat_stale(db_path=db, max_age_seconds=120) is True

    def test_no_heartbeat_recorded_at_all_is_stale(self, tmp_path):
        """A worker that has never written a heartbeat (never started, or
        crashed before its first iteration) must be treated as stale --
        never as "not stale because we have no evidence either way"."""
        import run_worker

        db = tmp_path / "h.db"
        initialize_schema(db)

        assert run_worker.is_worker_heartbeat_stale(db_path=db, max_age_seconds=120) is True
