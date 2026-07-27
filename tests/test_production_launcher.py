"""
tests/test_production_launcher.py — F14: production_launcher unit tests
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.queue import process_next


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
    """
    Manual Queue Submission — as of the Minimum Operational Dashboard
    Phase 0 correction, launch_episode_from_dashboard() never executes a
    pipeline directly. It validates the request, then places the episode
    on the same canonical queue the Scheduler uses:

        dashboard_control.enqueue_topic() -> queue.enqueue()
        -> run_worker (poll loop) -> queue.process_next()
        -> worker_integration (gated executor) -> pipeline.run_pipeline()

    These tests replace the old subprocess/Popen assertions with direct
    proof of that queue-injection behavior and its absence of any
    execution-orchestration side effect.
    """

    def _launch(self, db):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("hypogeum.channel_config.get_channel_profile", return_value=_active_channel()):
            from hypogeum.production_launcher import launch_episode_from_dashboard
            return launch_episode_from_dashboard(
                "ch_test", "ancient rome", "long_video", "balanced", ["youtube"],
                db_path=db,
            )

    def test_returns_launched_status(self, db, full_env):
        result = self._launch(db)
        assert result["status"] == "launched"

    def test_no_subprocess_popen_invoked(self, db, full_env):
        """Regression: the entire point of this correction is that no
        process is ever spawned for a manual submission."""
        with patch("subprocess.Popen") as mock_popen:
            self._launch(db)
        mock_popen.assert_not_called()

    def test_no_direct_run_pipeline_invocation(self, db, full_env):
        """Regression: launch_episode_from_dashboard() must never call
        hypogeum.pipeline.run_pipeline() itself — only the worker's own
        process_next() call site may do that, later, once claimed."""
        import hypogeum.pipeline as _pl
        with patch.object(
            _pl, "run_pipeline",
            side_effect=AssertionError("run_pipeline must not be called by production_launcher"),
        ):
            result = self._launch(db)
        assert result["status"] == "launched"

    def test_exactly_one_episode_topics_row_created(self, db, full_env):
        result = self._launch(db)
        conn = get_connection(db)
        try:
            rows = conn.execute(
                "SELECT topic_id, episode_id FROM episode_topics WHERE channel_id = ?",
                ("ch_test",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["episode_id"] == result["episode_id"]

    def test_exactly_one_run_queue_row_created(self, db, full_env):
        result = self._launch(db)
        conn = get_connection(db)
        try:
            rows = conn.execute(
                "SELECT queue_id, episode_id, channel_id, source, status "
                "FROM run_queue WHERE channel_id = ?",
                ("ch_test",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["episode_id"] == result["episode_id"]
        assert rows[0]["channel_id"] == "ch_test"
        assert rows[0]["source"] == "manual"
        assert rows[0]["status"] == "queued"

    def test_episode_topics_and_run_queue_linked_by_same_episode_id(self, db, full_env):
        """The two rows created by the two calls (enqueue_topic +
        queue.enqueue) must reference the identical episode_id — proving
        they are the same submission, not two independent/duplicated ones."""
        result = self._launch(db)
        conn = get_connection(db)
        try:
            topic_row = conn.execute(
                "SELECT episode_id FROM episode_topics WHERE channel_id = ?",
                ("ch_test",),
            ).fetchone()
            queue_row = conn.execute(
                "SELECT episode_id FROM run_queue WHERE channel_id = ?",
                ("ch_test",),
            ).fetchone()
        finally:
            conn.close()
        assert topic_row["episode_id"] == queue_row["episode_id"] == result["episode_id"]

    def test_queued_item_proceeds_only_through_canonical_worker_path(self, db, full_env):
        """The item this creates is indistinguishable from any other
        manually-enqueued item once the worker's real process_next() picks
        it up — proving there is no parallel/special-cased execution path
        for Create-page submissions."""
        result = self._launch(db)
        from hypogeum.calog import set_channel_state
        set_channel_state("ch_test", "active", db_path=db)

        mock_pipeline = MagicMock()
        with patch("hypogeum.pipeline.run_pipeline", mock_pipeline):
            pr = process_next(
                worker_id="w1", db_path=db, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert pr.status == "completed"
        mock_pipeline.assert_called_once()
        _, kwargs = mock_pipeline.call_args
        assert kwargs.get("episode_id") == result["episode_id"]
        assert kwargs.get("channel_id") == "ch_test"
        assert kwargs.get("topic_input") == "ancient rome"

    def test_result_contains_episode_id(self, db, full_env):
        result = self._launch(db)
        assert "episode_id" in result
        assert result["episode_id"].startswith("ep_")

    def test_no_mock_analytics_created(self, db, full_env):
        with patch("hypogeum.analytics_store.seed_mock_analytics") as mock_seed:
            self._launch(db)
        mock_seed.assert_not_called()
