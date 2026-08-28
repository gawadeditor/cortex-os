"""
tests/test_regenerate_coverage_plan_cli.py — Unit tests for
tools/regenerate_coverage_plan.py (2026-08-09, first-batch probe correction)

Loaded via importlib (tools/ has no __init__.py, matching this repo's
existing standalone-script convention). regenerate_cinematic_coverage_plan()
itself is always mocked here — this file only proves argument wiring and
exit-code behavior, never makes a real call and never touches real episode
files under .hypogeum-assets/.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _ROOT / "tools" / "regenerate_coverage_plan.py"

_spec = importlib.util.spec_from_file_location("regenerate_coverage_plan_cli", _SCRIPT_PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture(autouse=True)
def _noop_plan_weekly_reservation(monkeypatch):
    """2026-08-26 plan-boundary rule: the CLI's --force path now requires
    --channel-id and establishes/replays the verified persisted plan's
    weekly reservation before any LLM call. CLI wiring tests patch the
    regenerate function itself (so the real boundary never runs); the
    gating behavior is covered in tests/test_cinematic_director.py::
    TestPlanReservationGating and
    tests/test_plan_reservation_boundaries.py. The --channel-id-required
    parser rule has its own dedicated test below (zero network by
    construction)."""
    import hypogeum.plan_reservation as pr

    monkeypatch.setattr(
        pr, "ensure_verified_plan_weekly_reservation", lambda **kw: None,
    )
    yield


def _write_episode_fixtures(episode_dir: Path, episode_id: str) -> None:
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / f"script_package_{episode_id}.json").write_text(json.dumps({
        "sections": [{"section_id": "s_001", "title": "T",
                      "narration_beats": [{"beat_id": "b_001", "text": "one"}]}],
        "shorts_scripts": [],
    }))
    (episode_dir / f"narration_package_{episode_id}.json").write_text(json.dumps({
        "narration_segments": [{"beat_reference": "b_001", "duration_seconds": 8.0}],
    }))
    (episode_dir / f"visual_package_{episode_id}.json").write_text(json.dumps({
        "episode_metadata": {"episode_title": "T"},
        "scenes": [{"scene_id": "s01", "narration_reference": "b_001",
                    "source_type": "main_episode", "visual_spec": {},
                    "presentation_plan": {}, "narrative_signals": []}],
    }))


def _run_cli_no_channel(tmp_path: Path, episode_id: str, argv: list) -> int:
    """Same as _run_cli but deliberately WITHOUT --channel-id (for tests
    proving the parser refuses --force without it)."""
    episode_dir = tmp_path / "episodes" / episode_id
    _write_episode_fixtures(episode_dir, episode_id)
    full_argv = [
        "regenerate_coverage_plan.py",
        "--episode-id", episode_id,
        "--assets-root", str(tmp_path),
        *argv,
    ]
    with patch.object(sys, "argv", full_argv):
        try:
            cli.main()
            return 0
        except SystemExit as exc:
            return exc.code or 0


def _run_cli(tmp_path: Path, episode_id: str, argv: list) -> int:
    episode_dir = tmp_path / "episodes" / episode_id
    _write_episode_fixtures(episode_dir, episode_id)
    full_argv = [
        "regenerate_coverage_plan.py",
        "--episode-id", episode_id,
        "--assets-root", str(tmp_path),
        # 2026-08-26 plan-boundary rule: --force requires --channel-id; the
        # boundary helper itself is patched out by the autouse fixture.
        "--channel-id", "ch_test",
        *argv,
    ]
    with patch.object(sys, "argv", full_argv):
        try:
            cli.main()
            return 0
        except SystemExit as exc:
            return exc.code or 0


class TestProbeFirstBatchFlagWiring:

    def test_probe_flag_passes_true_to_regenerate(self, tmp_path, capsys):
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
            return_value={
                "status": "probe_succeeded", "reason": None,
                "active_plan_path": "x", "backup_path": None, "candidate_path": None,
                "plan": None, "totals": None,
                "probe_batch_index": 1, "probe_batch_count": 6, "probe_batch_beat_ids": ["b_001"],
            },
        ) as mock_regen:
            exit_code = _run_cli(tmp_path, "ep_test", ["--force", "--probe-first-batch"])

        assert mock_regen.call_args.kwargs["probe_first_batch"] is True
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "status:            probe_succeeded" in out
        assert "probe_batch_index: 1 of 6" in out

    def test_omitted_flag_passes_false_to_regenerate(self, tmp_path):
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
            return_value={
                "status": "installed", "reason": None,
                "active_plan_path": "x", "backup_path": None, "candidate_path": "x",
                "plan": {}, "totals": {},
            },
        ) as mock_regen:
            _run_cli(tmp_path, "ep_test", ["--force"])

        assert mock_regen.call_args.kwargs["probe_first_batch"] is False

    def test_probe_rejected_exits_nonzero(self, tmp_path):
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
            return_value={
                "status": "probe_rejected", "reason": "llm_call_failed: batch 1/6: HardFailure: 400",
                "active_plan_path": "x", "backup_path": None, "candidate_path": None,
                "plan": None, "totals": None,
                "probe_batch_index": 1, "probe_batch_count": 6, "probe_batch_beat_ids": ["b_001"],
            },
        ):
            exit_code = _run_cli(tmp_path, "ep_test", ["--force", "--probe-first-batch"])

        assert exit_code == 1

    def test_probe_succeeded_exits_zero(self, tmp_path):
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
            return_value={
                "status": "probe_succeeded", "reason": None,
                "active_plan_path": "x", "backup_path": None, "candidate_path": None,
                "plan": None, "totals": None,
                "probe_batch_index": 1, "probe_batch_count": 6, "probe_batch_beat_ids": ["b_001"],
            },
        ):
            exit_code = _run_cli(tmp_path, "ep_test", ["--force", "--probe-first-batch"])

        assert exit_code == 0

    def test_oversized_beat_value_error_caught_and_reported(self, tmp_path, capsys):
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
            side_effect=ValueError("beat 'b_001' has a shot budget of 30, which exceeds max_slots_per_batch=25"),
        ):
            exit_code = _run_cli(tmp_path, "ep_test", ["--force"])

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "BATCH PARTITIONING FAILED" in err

    def test_force_requires_channel_id_zero_network_without_it(self, tmp_path, capsys):
        """2026-08-26 plan-boundary rule: --force without --channel-id is a
        parser error BEFORE any code runs — regenerate_cinematic_coverage_
        plan() is never reached, so zero network calls are possible."""
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
        ) as mock_regen:
            exit_code = _run_cli_no_channel(tmp_path, "ep_test", ["--force"])
        assert exit_code != 0
        mock_regen.assert_not_called()
        err = capsys.readouterr().err
        assert "--channel-id" in err

    def test_probe_never_calls_a_second_time(self, tmp_path):
        """The CLI itself must never retry a probe -- one call to
        regenerate_cinematic_coverage_plan() maximum for one CLI
        invocation, success or failure."""
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
            return_value={
                "status": "probe_rejected", "reason": "x",
                "active_plan_path": "x", "backup_path": None, "candidate_path": None,
                "plan": None, "totals": None,
                "probe_batch_index": 1, "probe_batch_count": 6, "probe_batch_beat_ids": ["b_001"],
            },
        ) as mock_regen:
            _run_cli(tmp_path, "ep_test", ["--force", "--probe-first-batch"])

        assert mock_regen.call_count == 1

    def test_normal_regeneration_unaffected_when_flag_omitted(self, tmp_path, capsys):
        """Ordinary, non-probe regeneration output/behavior is unchanged
        when --probe-first-batch is never passed."""
        with patch.object(
            cli, "regenerate_cinematic_coverage_plan",
            return_value={
                "status": "installed", "reason": None,
                "active_plan_path": "x", "backup_path": "y", "candidate_path": "x",
                "plan": {"episode_id": "ep_test"}, "totals": {"total_shots": 130},
            },
        ):
            exit_code = _run_cli(tmp_path, "ep_test", ["--force", "--verify-ep-f3619022-fixture"])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "status:            installed" in out
        assert "probe_batch_index" not in out
