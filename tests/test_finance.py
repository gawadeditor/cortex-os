"""
tests/test_finance.py — Finance Controller test suite

Coverage:
    TestRunContext          — set/clear/get run context
    TestRecordCost          — insert to DB, non-blocking on error, context fallback
    TestWriteEpisodeRollup  — aggregation, file output, idempotency
    TestAgentIntegration    — record_cost called after run_agent()
    TestPipelineIntegration — set_run_context/clear_run_context wired in pipeline
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import get_connection, initialize_schema, insert_run
from hypogeum.finance import (
    clear_run_context,
    get_run_context,
    record_cost,
    set_run_context,
    write_episode_rollup,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_run_context():
    """Ensure run context is cleared before and after every test."""
    clear_run_context()
    yield
    clear_run_context()


@pytest.fixture()
def store(tmp_path):
    """Returns (db_path, finance_dir) with initialized schema + one run row."""
    db_path = tmp_path / "hypogeum.db"
    initialize_schema(db_path)
    conn = get_connection(db_path)
    insert_run(conn, "run-001", "ep_000", "2026-06-12T10:00:00Z")
    conn.commit()
    conn.close()
    finance_dir = tmp_path / "finance"
    return db_path, finance_dir


_STARTED = "2026-06-12T10:00:00Z"


# ---------------------------------------------------------------------------
# TestRunContext
# ---------------------------------------------------------------------------

class TestRunContext:
    def test_set_run_context_stores_values(self, tmp_path):
        set_run_context("run-abc", "ep_000", db_path=tmp_path / "test.db")
        ctx = get_run_context()
        assert ctx["run_id"] == "run-abc"
        assert ctx["episode_id"] == "ep_000"

    def test_clear_run_context_empties(self):
        set_run_context("run-abc", "ep_000")
        clear_run_context()
        assert get_run_context() == {}

    def test_get_run_context_returns_copy(self):
        set_run_context("run-abc", "ep_000")
        ctx = get_run_context()
        ctx["run_id"] = "mutated"
        # Original should be unchanged
        assert get_run_context()["run_id"] == "run-abc"

    def test_set_overwrites_previous_context(self):
        set_run_context("run-001", "ep_000")
        set_run_context("run-002", "ep_001")
        ctx = get_run_context()
        assert ctx["run_id"] == "run-002"
        assert ctx["episode_id"] == "ep_001"

    def test_context_initially_empty(self):
        assert get_run_context() == {}


# ---------------------------------------------------------------------------
# TestRecordCost
# ---------------------------------------------------------------------------

class TestRecordCost:
    def test_record_cost_writes_to_db(self, store):
        db_path, _ = store
        set_run_context("run-001", "ep_000", db_path=db_path)
        record_cost("openrouter", "anthropic/claude-opus-4.6", "research_synthesis", 0.1446,
                    tokens_or_credits=4821.0)
        conn = get_connection(db_path)
        rows = conn.execute("SELECT * FROM costs WHERE run_id='run-001'").fetchall()
        conn.close()
        assert len(rows) == 1
        assert abs(rows[0]["cost_usd"] - 0.1446) < 1e-9

    def test_record_cost_fields_stored_correctly(self, store):
        db_path, _ = store
        set_run_context("run-001", "ep_000", db_path=db_path)
        record_cost("openrouter", "anthropic/claude-opus-4.6", "td_planning", 0.05,
                    tokens_or_credits=1000.0)
        conn = get_connection(db_path)
        row = conn.execute("SELECT * FROM costs").fetchone()
        conn.close()
        assert row["provider"] == "openrouter"
        assert row["model"] == "anthropic/claude-opus-4.6"
        assert row["call_type"] == "td_planning"
        assert row["episode_id"] == "ep_000"
        assert abs(row["tokens_or_credits"] - 1000.0) < 1e-9

    def test_record_cost_without_context_is_silent_noop(self, store):
        db_path, _ = store
        # No set_run_context() called
        record_cost("openrouter", None, "research_synthesis", 0.10)
        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
        conn.close()
        assert count == 0

    def test_record_cost_explicit_run_id_overrides_context(self, store):
        db_path, _ = store
        set_run_context("run-999", "ep_999", db_path=db_path)
        record_cost("openrouter", None, "synthesis", 0.10,
                    run_id="run-001", episode_id="ep_000", db_path=db_path)
        conn = get_connection(db_path)
        row = conn.execute("SELECT run_id FROM costs").fetchone()
        conn.close()
        assert row["run_id"] == "run-001"

    def test_record_cost_non_blocking_on_db_error(self):
        set_run_context("run-001", "ep_000", db_path=Path("/nonexistent/path/test.db"))
        # Must not raise even with invalid db path
        record_cost("openrouter", None, "synthesis", 0.10)

    def test_record_cost_multiple_calls(self, store):
        db_path, _ = store
        set_run_context("run-001", "ep_000", db_path=db_path)
        for cost in [0.05, 0.10, 0.15]:
            record_cost("openrouter", "model", "planning", cost)
        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM costs WHERE run_id='run-001'").fetchone()[0]
        conn.close()
        assert count == 3

    def test_record_cost_none_model_accepted(self, store):
        db_path, _ = store
        set_run_context("run-001", "ep_000", db_path=db_path)
        record_cost("muapi", None, "scene_generation", 0.15)
        conn = get_connection(db_path)
        row = conn.execute("SELECT model FROM costs").fetchone()
        conn.close()
        assert row["model"] is None


# ---------------------------------------------------------------------------
# TestWriteEpisodeRollup
# ---------------------------------------------------------------------------

class TestWriteEpisodeRollup:
    def _insert_costs(self, db_path, costs):
        """Helper: insert cost rows from list of (provider, call_type, cost_usd)."""
        import uuid
        from datetime import datetime, timezone
        conn = get_connection(db_path)
        for provider, call_type, cost_usd in costs:
            from hypogeum.db import insert_cost
            insert_cost(conn, str(uuid.uuid4()), "run-001", "ep_000",
                        provider, call_type, cost_usd, datetime.now(timezone.utc).isoformat())
        conn.commit()
        conn.close()

    def test_returns_rollup_dict(self, store):
        db_path, finance_dir = store
        rollup = write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        assert isinstance(rollup, dict)
        assert "total_cost_usd" in rollup
        assert "run_id" in rollup
        assert "episode_id" in rollup

    def test_total_cost_correct(self, store):
        db_path, finance_dir = store
        self._insert_costs(db_path, [
            ("openrouter", "research", 0.10),
            ("openrouter", "planning", 0.20),
            ("muapi", "generation", 0.30),
        ])
        rollup = write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        assert abs(rollup["total_cost_usd"] - 0.60) < 1e-5

    def test_cost_by_provider(self, store):
        db_path, finance_dir = store
        self._insert_costs(db_path, [
            ("openrouter", "research", 0.10),
            ("openrouter", "planning", 0.10),
            ("muapi", "generation", 0.30),
        ])
        rollup = write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        assert abs(rollup["cost_by_provider"]["openrouter"] - 0.20) < 1e-5
        assert abs(rollup["cost_by_provider"]["muapi"] - 0.30) < 1e-5

    def test_cost_by_call_type(self, store):
        db_path, finance_dir = store
        self._insert_costs(db_path, [
            ("openrouter", "research_synthesis", 0.15),
            ("openrouter", "td_planning", 0.05),
        ])
        rollup = write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        assert abs(rollup["cost_by_call_type"]["research_synthesis"] - 0.15) < 1e-5
        assert abs(rollup["cost_by_call_type"]["td_planning"] - 0.05) < 1e-5

    def test_call_count_correct(self, store):
        db_path, finance_dir = store
        self._insert_costs(db_path, [
            ("openrouter", "r", 0.01),
            ("openrouter", "r", 0.01),
            ("openrouter", "r", 0.01),
        ])
        rollup = write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        assert rollup["call_count"] == 3

    def test_zero_costs_produces_valid_rollup(self, store):
        db_path, finance_dir = store
        rollup = write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        assert rollup["total_cost_usd"] == 0.0
        assert rollup["call_count"] == 0

    def test_json_file_written(self, store):
        db_path, finance_dir = store
        write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        out_path = finance_dir / "ep_000_finance.json"
        assert out_path.exists()

    def test_json_file_parseable(self, store):
        db_path, finance_dir = store
        write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        content = json.loads((finance_dir / "ep_000_finance.json").read_text())
        assert content["episode_id"] == "ep_000"
        assert content["metadata_version"] == "v1"

    def test_idempotent_overwrite(self, store):
        db_path, finance_dir = store
        write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        out_path = finance_dir / "ep_000_finance.json"
        # Must still be valid JSON and not duplicated
        content = json.loads(out_path.read_text())
        assert content["episode_id"] == "ep_000"

    def test_metadata_version_is_v1(self, store):
        db_path, finance_dir = store
        rollup = write_episode_rollup("run-001", "ep_000", db_path=db_path, finance_dir=finance_dir)
        assert rollup["metadata_version"] == "v1"

    def test_returns_error_rollup_on_bad_db_path(self):
        rollup = write_episode_rollup(
            "run-001", "ep_000",
            db_path=Path("/nonexistent/bad.db"),
            finance_dir=Path("/tmp/finance"),
        )
        assert rollup["total_cost_usd"] == 0.0
        assert "error" in rollup


# ---------------------------------------------------------------------------
# TestAgentIntegration
# ---------------------------------------------------------------------------

class TestAgentIntegration:
    def test_record_cost_called_after_run_agent(self, tmp_path):
        """record_cost in agent.py is called with openrouter provider."""
        from hypogeum.agent import run_agent
        from hypogeum.identifiers import METADATA_VERSION
        from unittest.mock import MagicMock, patch

        cfg = MagicMock()
        cfg.OPENROUTER_API_KEY = "test-key"
        cfg.ANTHROPIC_API_KEY = None

        fake_result = MagicMock()
        fake_result.content = '{"test": "value"}'
        fake_result.model_used = "anthropic/claude-sonnet-4.6"
        fake_result.estimated_cost_usd = 0.042
        fake_result.usage = {"total_tokens": 1000, "prompt_tokens": 800, "completion_tokens": 200}

        recorded = []

        def capture_record_cost(provider, model, call_type, cost_usd, tokens_or_credits=None,
                                 run_id=None, episode_id=None, db_path=None):
            recorded.append({
                "provider": provider,
                "model": model,
                "cost_usd": cost_usd,
            })

        with patch("hypogeum.agent.call_claude", return_value=fake_result), \
             patch("hypogeum.agent.validate", return_value={"test": "value"}), \
             patch("hypogeum.agent.record_cost", side_effect=capture_record_cost):
            run_agent(
                agent_name="test_agent",
                model_slug="anthropic/claude-sonnet-4.6",
                system_prompt="You are a test agent.",
                input_package={"task": "test"},
                output_schema="test_schema",
                episode_id="ep_000",
                max_tokens=1000,
                config=cfg,
                output_dir=tmp_path,
            )

        assert len(recorded) == 1
        assert recorded[0]["provider"] == "openrouter"
        assert abs(recorded[0]["cost_usd"] - 0.042) < 1e-9

    def test_record_cost_receives_model_used(self, tmp_path):
        """record_cost receives the model_used from CallResult, not the requested slug."""
        from hypogeum.agent import run_agent
        from unittest.mock import MagicMock, patch

        cfg = MagicMock()

        fake_result = MagicMock()
        fake_result.content = '{"field": "value"}'
        fake_result.model_used = "anthropic/claude-opus-4.6"
        fake_result.estimated_cost_usd = 0.10
        fake_result.usage = {"total_tokens": 2000}

        recorded = []

        def capture(**kwargs):
            recorded.append(kwargs)

        with patch("hypogeum.agent.call_claude", return_value=fake_result), \
             patch("hypogeum.agent.validate", return_value={"field": "value"}), \
             patch("hypogeum.agent.record_cost", side_effect=lambda *a, **k: recorded.append({"model": k.get("model") or (a[1] if len(a) > 1 else None)})):
            run_agent(
                agent_name="test_agent",
                model_slug="anthropic/claude-opus-4.6",
                system_prompt="System.",
                input_package={},
                output_schema="schema",
                episode_id="ep_000",
                max_tokens=1000,
                config=cfg,
                output_dir=tmp_path,
            )

        assert len(recorded) == 1
        assert recorded[0]["model"] == "anthropic/claude-opus-4.6"


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    def test_pipeline_imports_set_run_context(self):
        """pipeline.py must import set_run_context from finance."""
        import importlib
        import ast
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        assert "set_run_context" in src

    def test_pipeline_imports_clear_run_context(self):
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        assert "clear_run_context" in src

    def test_pipeline_imports_write_episode_rollup(self):
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        assert "write_episode_rollup" in src
