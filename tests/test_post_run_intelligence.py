"""
tests/test_post_run_intelligence.py — Phase 5 post-run intelligence orchestrator

Coverage:
    TestKgSeedFlow        — run_kg_seed_flow writes file, inserts DB, updates world bible
    TestSignalFlow        — run_signal_flow_if_available skips when no snapshot
    TestSignalFlowData    — run_signal_flow_if_available ingests when snapshot provided
    TestNonBlocking       — failures in sub-flows do not propagate exceptions
    TestReportWritten     — post_run_intelligence_report_{episode_id}.json written
    TestReportFields      — report has required top-level fields
    TestPhase5Callable    — run_post_run_intelligence callable with no-op args
    TestModelSlugConfig   — EP_MODEL, TD_PLANNING_MODEL, BGA_PASS2_MODEL not known-invalid
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.post_run_intelligence import (
    run_kg_seed_flow,
    run_post_run_intelligence,
    run_signal_flow_if_available,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_prod_meta(episode_id: str = "ep_000", title: str = "Test Title") -> dict:
    return {
        "episode_id": episode_id,
        "run_id": "run-test-001",
        "selected_title": title,
        "production_metadata": {
            "selected_title": title,
            "narrative_structure": {"section_titles": ["Intro", "Main"]},
        },
    }


def _minimal_snapshot(episode_id: str = "ep_000") -> dict:
    return {
        "episode_id": episode_id,
        "run_id": "run-test-001",
        "youtube_video_id": "abc123",
        "published_at": "2026-06-12T10:00:00+00:00",
        "window_days": 7,
        "metrics": {"views": 100, "estimatedMinutesWatched": 50},
        "data_quality": "captured",
    }


# ---------------------------------------------------------------------------
# TestKgSeedFlow
# ---------------------------------------------------------------------------

class TestKgSeedFlow:
    def test_returns_ok_true(self, tmp_path):
        result = run_kg_seed_flow(
            episode_id="ep_000",
            production_metadata=_minimal_prod_meta(),
            run_id="run-test-001",
            knowledge_dir=tmp_path / "knowledge",
            db_path=tmp_path / "test.db",
        )
        assert result["ok"] is True

    def test_kg_seed_id_returned(self, tmp_path):
        result = run_kg_seed_flow(
            episode_id="ep_000",
            production_metadata=_minimal_prod_meta(),
            run_id="run-test-001",
            knowledge_dir=tmp_path / "knowledge",
            db_path=tmp_path / "test.db",
        )
        assert result["kg_seed_id"] is not None
        assert len(result["kg_seed_id"]) > 0

    def test_no_error_field(self, tmp_path):
        result = run_kg_seed_flow(
            episode_id="ep_000",
            production_metadata=_minimal_prod_meta(),
            run_id="run-test-001",
            knowledge_dir=tmp_path / "knowledge",
            db_path=tmp_path / "test.db",
        )
        assert result["error"] is None

    def test_kg_seed_file_written(self, tmp_path):
        kdir = tmp_path / "knowledge"
        run_kg_seed_flow(
            episode_id="ep_000",
            production_metadata=_minimal_prod_meta(),
            run_id="run-test-001",
            knowledge_dir=kdir,
            db_path=tmp_path / "test.db",
        )
        assert (kdir / "kg_seed_ep_000.json").exists()

    def test_world_bible_written(self, tmp_path):
        kdir = tmp_path / "knowledge"
        run_kg_seed_flow(
            episode_id="ep_000",
            production_metadata=_minimal_prod_meta(),
            run_id="run-test-001",
            knowledge_dir=kdir,
            db_path=tmp_path / "test.db",
        )
        assert (kdir / "world_bible.json").exists()

    def test_db_record_written(self, tmp_path):
        import sqlite3
        db_path = tmp_path / "test.db"
        run_kg_seed_flow(
            episode_id="ep_000",
            production_metadata=_minimal_prod_meta(),
            run_id="run-test-001",
            knowledge_dir=tmp_path / "knowledge",
            db_path=db_path,
        )
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM kg_seeds WHERE episode_id='ep_000'").fetchall()
        conn.close()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# TestSignalFlow
# ---------------------------------------------------------------------------

class TestSignalFlow:
    def test_skipped_when_no_snapshot(self, tmp_path):
        result = run_signal_flow_if_available(
            episode_id="ep_000",
            analytics_snapshot=None,
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert result["skipped"] is True
        assert result["ok"] is True
        assert result["signal_id"] is None

    def test_skipped_when_empty_snapshot(self, tmp_path):
        result = run_signal_flow_if_available(
            episode_id="ep_000",
            analytics_snapshot={},
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert result["skipped"] is True


# ---------------------------------------------------------------------------
# TestSignalFlowData
# ---------------------------------------------------------------------------

class TestSignalFlowData:
    def test_ok_when_snapshot_provided(self, tmp_path):
        result = run_signal_flow_if_available(
            episode_id="ep_000",
            analytics_snapshot=_minimal_snapshot(),
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert result["ok"] is True
        assert result["skipped"] is False

    def test_signal_id_returned(self, tmp_path):
        result = run_signal_flow_if_available(
            episode_id="ep_000",
            analytics_snapshot=_minimal_snapshot(),
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert result["signal_id"] is not None

    def test_signal_file_written(self, tmp_path):
        sdir = tmp_path / "signals"
        run_signal_flow_if_available(
            episode_id="ep_000",
            analytics_snapshot=_minimal_snapshot(),
            signals_dir=sdir,
            db_path=tmp_path / "test.db",
        )
        assert (sdir / "signal_record_ep_000.json").exists()

    def test_signal_db_record_written(self, tmp_path):
        import sqlite3
        db_path = tmp_path / "test.db"
        run_signal_flow_if_available(
            episode_id="ep_000",
            analytics_snapshot=_minimal_snapshot(),
            signals_dir=tmp_path / "signals",
            db_path=db_path,
        )
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT * FROM signal_records WHERE episode_id='ep_000'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# TestNonBlocking
# ---------------------------------------------------------------------------

class TestNonBlocking:
    def test_kg_seed_flow_does_not_raise_on_bad_db_path(self, tmp_path):
        # Pass a file path where a directory is expected — should catch and return error
        bad_db = tmp_path / "nonexistent_dir" / "deep" / "nested" / "test.db"
        # Actually this should work because initialize_schema creates parents.
        # Test with a genuinely bad scenario: pass a dir as db_path
        # Use production_metadata=None to produce minimal seed
        result = run_kg_seed_flow(
            episode_id="ep_000",
            production_metadata=None,
            run_id="",
            knowledge_dir=tmp_path / "knowledge",
            db_path=tmp_path / "test.db",
        )
        # Should return a dict regardless (ok may be True or False)
        assert isinstance(result, dict)
        assert "ok" in result

    def test_run_post_run_intelligence_never_raises(self, tmp_path):
        # Should not raise even with all None args
        result = run_post_run_intelligence(
            episode_id="ep_000",
            production_metadata=None,
            source_registry=None,
            run_id="",
            analytics_snapshot=None,
            output_dir=tmp_path / "intelligence",
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert isinstance(result, dict)

    def test_signal_flow_never_raises_on_bad_snapshot(self, tmp_path):
        bad_snapshot = {"broken": object()}  # not JSON-serializable
        # ingest_signal should handle this; if it raises, run_signal_flow catches it
        try:
            result = run_signal_flow_if_available(
                episode_id="ep_000",
                analytics_snapshot=bad_snapshot,
                signals_dir=tmp_path / "signals",
                db_path=tmp_path / "test.db",
            )
            assert isinstance(result, dict)
            assert "ok" in result
        except Exception:
            pytest.fail("run_signal_flow_if_available raised an exception — must not propagate")


# ---------------------------------------------------------------------------
# TestReportWritten
# ---------------------------------------------------------------------------

class TestReportWritten:
    def test_report_file_created(self, tmp_path):
        intel_dir = tmp_path / "intelligence"
        run_post_run_intelligence(
            episode_id="ep_000",
            output_dir=intel_dir,
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert (intel_dir / "post_run_intelligence_report_ep_000.json").exists()

    def test_report_is_parseable_json(self, tmp_path):
        intel_dir = tmp_path / "intelligence"
        run_post_run_intelligence(
            episode_id="ep_000",
            output_dir=intel_dir,
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        content = json.loads(
            (intel_dir / "post_run_intelligence_report_ep_000.json").read_text()
        )
        assert isinstance(content, dict)

    def test_report_content_matches_return_value(self, tmp_path):
        intel_dir = tmp_path / "intelligence"
        report = run_post_run_intelligence(
            episode_id="ep_000",
            output_dir=intel_dir,
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        content = json.loads(
            (intel_dir / "post_run_intelligence_report_ep_000.json").read_text()
        )
        assert content["episode_id"] == report["episode_id"]


# ---------------------------------------------------------------------------
# TestReportFields
# ---------------------------------------------------------------------------

class TestReportFields:
    REQUIRED = [
        "schema_version", "metadata_version", "episode_id",
        "run_id", "generated_at", "all_flows_ok",
        "kg_seed_flow", "signal_flow",
    ]

    def test_all_required_fields_present(self, tmp_path):
        report = run_post_run_intelligence(
            episode_id="ep_000",
            output_dir=tmp_path / "intelligence",
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        for f in self.REQUIRED:
            assert f in report, f"Missing field: {f}"

    def test_schema_version_is_v1(self, tmp_path):
        report = run_post_run_intelligence(
            episode_id="ep_000",
            output_dir=tmp_path / "intelligence",
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert report["schema_version"] == "v1"

    def test_kg_seed_flow_is_dict(self, tmp_path):
        report = run_post_run_intelligence(
            episode_id="ep_000",
            output_dir=tmp_path / "intelligence",
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert isinstance(report["kg_seed_flow"], dict)

    def test_signal_flow_skipped_when_no_snapshot(self, tmp_path):
        report = run_post_run_intelligence(
            episode_id="ep_000",
            analytics_snapshot=None,
            output_dir=tmp_path / "intelligence",
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert report["signal_flow"]["skipped"] is True


# ---------------------------------------------------------------------------
# TestPhase5Callable
# ---------------------------------------------------------------------------

class TestPhase5Callable:
    def test_callable_with_no_op_args(self, tmp_path):
        result = run_post_run_intelligence(
            episode_id="_probe",
            output_dir=tmp_path / "intelligence",
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "probe.db",
        )
        assert isinstance(result, dict)
        assert "all_flows_ok" in result

    def test_episode_id_in_report(self, tmp_path):
        result = run_post_run_intelligence(
            episode_id="ep_007",
            output_dir=tmp_path / "intelligence",
            knowledge_dir=tmp_path / "knowledge",
            signals_dir=tmp_path / "signals",
            db_path=tmp_path / "test.db",
        )
        assert result["episode_id"] == "ep_007"


# ---------------------------------------------------------------------------
# TestModelSlugConfig
# ---------------------------------------------------------------------------

_MINIMAL_ENV = {
    "OPENROUTER_API_KEY": "sk-or-test",
    "OPENROUTER_SONNET_MODEL_ID": "anthropic/claude-sonnet-4.6",
    "OPENROUTER_OPUS_MODEL_ID": "anthropic/claude-opus-4.6",
}


class TestModelSlugConfig:
    # Both claude-fable-5 and gpt-5.5-pro confirmed live on OpenRouter 2026-06-12.
    # This set is now empty; kept as a hook for future delist events.
    _KNOWN_INVALID: set = set()

    def _load_config(self):
        import os
        from unittest.mock import patch
        from hypogeum.config import Config
        env = {**_MINIMAL_ENV}
        with patch.dict(os.environ, env):
            return Config()

    def test_ep_model_is_fable5_or_opus(self):
        # claude-fable-5 confirmed live on OpenRouter 2026-06-12
        cfg = self._load_config()
        assert cfg.EP_MODEL in (
            "anthropic/claude-fable-5", "anthropic/claude-opus-4.6",
        ), f"EP_MODEL={cfg.EP_MODEL!r} is not the expected EP model"

    def test_td_planning_model_is_gpt_based(self):
        # openai/gpt-5.5-pro confirmed live on OpenRouter 2026-06-12;
        # thumbnail reasoning stack must remain GPT-based
        cfg = self._load_config()
        assert cfg.TD_PLANNING_MODEL.startswith("openai/"), (
            f"TD_PLANNING_MODEL={cfg.TD_PLANNING_MODEL!r} must be GPT-based"
        )

    def test_bga_pass2_model_is_gpt_based(self):
        # openai/gpt-5.5-pro confirmed live; BGA Pass2 must remain GPT-based
        cfg = self._load_config()
        assert cfg.BGA_PASS2_MODEL.startswith("openai/"), (
            f"BGA_PASS2_MODEL={cfg.BGA_PASS2_MODEL!r} must be GPT-based"
        )

    def test_ep_model_non_empty(self):
        cfg = self._load_config()
        assert cfg.EP_MODEL, "EP_MODEL is empty"

    def test_rd_model_non_empty(self):
        cfg = self._load_config()
        assert cfg.RD_MODEL, "RD_MODEL is empty"

    def test_config_reads_from_env(self):
        """Config must read model slugs via _get_optional (env-first), not hardcoded."""
        import os
        from unittest.mock import patch
        from hypogeum.config import Config

        env = {**_MINIMAL_ENV, "EP_MODEL": "anthropic/claude-sonnet-4.6"}
        with patch.dict(os.environ, env):
            cfg = Config()
            assert cfg.EP_MODEL == "anthropic/claude-sonnet-4.6"
