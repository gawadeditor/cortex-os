"""
tests/test_scheduling_evidence_daemon_wiring.py — run_worker.py's
2026-08-23 wiring of hypogeum.publishing.evidence_based_scheduling into
the normal daemon loop (Phase 3 requirement: "the daemon must refresh
scheduling evidence automatically before compiling future schedules").

Covers:
  - _refresh_scheduling_evidence_for_all_channels() calls the refresh
    function once per active channel, isolates one channel's exception
    from another, and reports which channels were actually updated.
  - run_iteration() calls it on the SAME cadence as the existing
    analytics sync (every _ANALYTICS_SYNC_EVERY_N iterations), never on
    a dry_run iteration, and its result is surfaced in the returned dict.
"""

from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import initialize_schema


class TestRefreshSchedulingEvidenceForAllChannels:
    def test_calls_refresh_once_per_active_channel(self, tmp_path):
        import run_worker

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)

        ch_a, ch_b = MagicMock(), MagicMock()
        ch_a.channel_id, ch_b.channel_id = "ch_a", "ch_b"

        called_channels = []

        class _FakeResult:
            deliverables_updated = []

        def _fake_refresh(channel_id, db_path=None):
            called_channels.append(channel_id)
            return _FakeResult()

        with patch("hypogeum.channel_config.list_channel_profiles", return_value=[ch_a, ch_b]), \
             patch("hypogeum.publishing.evidence_based_scheduling.refresh_analytics_scheduling_policy", _fake_refresh):
            result = run_worker._refresh_scheduling_evidence_for_all_channels(db_path=db_path)

        assert set(called_channels) == {"ch_a", "ch_b"}
        assert result["channels_checked"] == 2

    def test_one_channel_exception_never_stops_another(self, tmp_path):
        import run_worker

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)

        ch_a, ch_b = MagicMock(), MagicMock()
        ch_a.channel_id, ch_b.channel_id = "ch_boom", "ch_ok"

        called_channels = []

        class _FakeResult:
            deliverables_updated = ["main_episode"]

        def _fake_refresh(channel_id, db_path=None):
            called_channels.append(channel_id)
            if channel_id == "ch_boom":
                raise RuntimeError("simulated failure")
            return _FakeResult()

        with patch("hypogeum.channel_config.list_channel_profiles", return_value=[ch_a, ch_b]), \
             patch("hypogeum.publishing.evidence_based_scheduling.refresh_analytics_scheduling_policy", _fake_refresh):
            result = run_worker._refresh_scheduling_evidence_for_all_channels(db_path=db_path)

        assert called_channels == ["ch_boom", "ch_ok"]
        assert result["channels_updated"] == ["ch_ok"]

    def test_reports_only_channels_with_actual_updates(self, tmp_path):
        import run_worker

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)

        ch_a = MagicMock()
        ch_a.channel_id = "ch_noop"

        class _FakeResult:
            deliverables_updated = []

        with patch("hypogeum.channel_config.list_channel_profiles", return_value=[ch_a]), \
             patch("hypogeum.publishing.evidence_based_scheduling.refresh_analytics_scheduling_policy", return_value=_FakeResult()):
            result = run_worker._refresh_scheduling_evidence_for_all_channels(db_path=db_path)

        assert result["channels_updated"] == []


class TestRunIterationSchedulingEvidenceCadence:
    def _base_run_iteration_kwargs(self, db_path, config):
        return dict(
            worker_id="w1", db_path=db_path, channel_id=None,
            config=config, bounded_run_id=None,
        )

    def test_refresh_called_on_analytics_sync_iteration_not_dry_run(self, tmp_path, monkeypatch):
        import run_worker

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)

        class _FakeClearance:
            cleared = True
            reason = None
            validation_exemption = None
        monkeypatch.setattr("hypogeum.factory_director.check_run_clearance", lambda **k: _FakeClearance())

        from hypogeum.queue import PollResult
        monkeypatch.setattr("hypogeum.queue.poll", lambda **k: PollResult())
        monkeypatch.setattr(run_worker, "_sync_analytics_background", lambda **k: None)

        called = {"n": 0}
        monkeypatch.setattr(
            run_worker, "_refresh_scheduling_evidence_for_all_channels",
            lambda **k: called.__setitem__("n", called["n"] + 1) or {"channels_checked": 0, "channels_updated": []},
        )

        result = run_worker.run_iteration(
            worker_id="w1", db_path=db_path, dry_run=False, channel_id=None,
            config=object(), iteration=run_worker._ANALYTICS_SYNC_EVERY_N,
        )

        assert called["n"] == 1
        assert result["scheduling_evidence"] == {"channels_checked": 0, "channels_updated": []}

    def test_refresh_not_called_on_non_sync_iteration(self, tmp_path, monkeypatch):
        import run_worker

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)

        class _FakeClearance:
            cleared = True
            reason = None
            validation_exemption = None
        monkeypatch.setattr("hypogeum.factory_director.check_run_clearance", lambda **k: _FakeClearance())

        from hypogeum.queue import PollResult
        monkeypatch.setattr("hypogeum.queue.poll", lambda **k: PollResult())

        called = {"n": 0}
        monkeypatch.setattr(
            run_worker, "_refresh_scheduling_evidence_for_all_channels",
            lambda **k: called.__setitem__("n", called["n"] + 1) or {},
        )

        result = run_worker.run_iteration(
            worker_id="w1", db_path=db_path, dry_run=False, channel_id=None,
            config=object(), iteration=1,  # not a multiple of _ANALYTICS_SYNC_EVERY_N
        )

        assert called["n"] == 0
        assert result["scheduling_evidence"] == {}

    def test_refresh_never_called_on_dry_run(self, tmp_path, monkeypatch):
        import run_worker

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)

        class _FakeClearance:
            cleared = True
            reason = None
            validation_exemption = None
        monkeypatch.setattr("hypogeum.factory_director.check_run_clearance", lambda **k: _FakeClearance())

        from hypogeum.queue import PollResult
        monkeypatch.setattr("hypogeum.queue.poll", lambda **k: PollResult())

        called = {"n": 0}
        monkeypatch.setattr(
            run_worker, "_refresh_scheduling_evidence_for_all_channels",
            lambda **k: called.__setitem__("n", called["n"] + 1) or {},
        )

        run_worker.run_iteration(
            worker_id="w1", db_path=db_path, dry_run=True, channel_id=None,
            config=object(), iteration=run_worker._ANALYTICS_SYNC_EVERY_N,
        )

        assert called["n"] == 0

    def test_refresh_sweep_exception_never_crashes_iteration(self, tmp_path, monkeypatch):
        import run_worker

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)

        class _FakeClearance:
            cleared = True
            reason = None
            validation_exemption = None
        monkeypatch.setattr("hypogeum.factory_director.check_run_clearance", lambda **k: _FakeClearance())

        from hypogeum.queue import PollResult
        monkeypatch.setattr("hypogeum.queue.poll", lambda **k: PollResult())
        monkeypatch.setattr(run_worker, "_sync_analytics_background", lambda **k: None)

        def _boom(**kwargs):
            raise RuntimeError("simulated sweep failure")
        monkeypatch.setattr(run_worker, "_refresh_scheduling_evidence_for_all_channels", _boom)

        result = run_worker.run_iteration(
            worker_id="w1", db_path=db_path, dry_run=False, channel_id=None,
            config=object(), iteration=run_worker._ANALYTICS_SYNC_EVERY_N,
        )

        assert result["scheduling_evidence"] == {}  # left at its default, never raised
        assert result["analytics_synced"] is True  # analytics sync itself still completed
