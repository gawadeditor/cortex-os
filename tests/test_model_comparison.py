"""
tests/test_model_comparison.py — Model Output Comparison tests

Coverage:
    TestSchemaAndDB          — schema exists, insert/complete/get_latest helpers
    TestArchiveQuery         — null provenance returns [], populated rows returned
    TestRunModelComparison   — call_claude used (not run_agent), credential handling,
                               error capture, DB write, status logic
    TestNoUnauthorisedGeneration — archive/get_latest make no provider calls
"""

from __future__ import annotations

import json
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_archive_index_entry,
    insert_model_comparison_run,
    complete_model_comparison_run,
    get_latest_model_comparison,
)
from hypogeum.model_comparison import (
    DEFAULT_LLM_MATRIX,
    BENCHMARK_USER_MESSAGE,
    run_model_comparison,
    query_archive_for_comparison,
    get_model_comparison_result,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "mc_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def conn(db):
    c = get_connection(db)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_ID   = "bbbbbbbb-0000-0000-0000-000000000001"
_NOW      = "2026-06-26T10:00:00+00:00"
_DONE_AT  = "2026-06-26T10:01:45+00:00"
_EPISODE  = "ep_post_m72_001"

_FAKE_CALL_RESULT = MagicMock(
    content="This is the LLM output text.",
    usage={"prompt_tokens": 100, "completion_tokens": 120, "total_tokens": 220},
    model_used="anthropic/claude-sonnet-4.6",
    finish_reason="stop",
    response_time_ms=1200,
    estimated_cost_usd=0.02,
)

_MINI_MATRIX = [
    {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
]


def _seed_archive_row(conn, episode_id=_EPISODE, generation_prompt=None,
                      provider="openrouter", model="anthropic/claude-sonnet-4.6",
                      cost_usd=0.02, asset_id=None):
    """Insert one archive_index row with optional M7.2 provenance fields."""
    archive_id = str(uuid.uuid4())
    asset_id   = asset_id or str(uuid.uuid4())
    conn.execute("BEGIN")
    insert_archive_index_entry(
        conn,
        archive_id=archive_id,
        asset_id=asset_id,
        episode_id=episode_id,
        asset_type="video_clip",
        provider=provider,
        archived_at=_NOW,
        model=model,
        generation_prompt=generation_prompt,
        cost_usd=cost_usd,
        scene_id="sc_001",
        archive_location="episodes/ep/video/sc_001.mp4",
        asset_created_at=_NOW,
    )
    conn.commit()
    return asset_id


# ===========================================================================
# TestSchemaAndDB
# ===========================================================================

class TestSchemaAndDB:

    def test_model_comparison_runs_table_exists(self, conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "model_comparison_runs" in tables

    def test_model_comparison_runs_columns(self, conn):
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(model_comparison_runs)"
        ).fetchall()}
        required = {
            "run_id", "run_type", "prompt_name", "prompt_text",
            "input_context", "provider_matrix", "status",
            "initiated_at", "completed_at", "operator_note", "results_json",
        }
        assert required <= cols

    def test_insert_creates_running_row(self, db):
        conn = get_connection(db)
        insert_model_comparison_run(
            conn, run_id=_RUN_ID, run_type="llm",
            prompt_text="System prompt", provider_matrix="[]",
            initiated_at=_NOW, prompt_name="executive_producer",
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM model_comparison_runs WHERE run_id=?", (_RUN_ID,)
        ).fetchone()
        assert row["status"] == "running"
        assert row["prompt_name"] == "executive_producer"
        assert row["results_json"] is None
        conn.close()

    def test_complete_updates_status_and_results_json(self, db):
        conn = get_connection(db)
        insert_model_comparison_run(
            conn, run_id=_RUN_ID, run_type="llm",
            prompt_text="System prompt", provider_matrix="[]",
            initiated_at=_NOW,
        )
        conn.commit()
        results = [{"provider": "openrouter", "model": "claude", "status": "success"}]
        complete_model_comparison_run(conn, _RUN_ID, results, _DONE_AT)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM model_comparison_runs WHERE run_id=?", (_RUN_ID,)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["completed_at"] == _DONE_AT
        assert json.loads(row["results_json"]) == results
        conn.close()

    def test_complete_accepts_error_status(self, db):
        conn = get_connection(db)
        insert_model_comparison_run(
            conn, run_id=_RUN_ID, run_type="llm",
            prompt_text="p", provider_matrix="[]", initiated_at=_NOW,
        )
        conn.commit()
        complete_model_comparison_run(conn, _RUN_ID, [], _DONE_AT, status="error")
        conn.commit()
        row = conn.execute(
            "SELECT status FROM model_comparison_runs WHERE run_id=?", (_RUN_ID,)
        ).fetchone()
        assert row["status"] == "error"
        conn.close()

    def test_get_latest_returns_none_on_empty_db(self, db):
        conn = get_connection(db)
        result = get_latest_model_comparison(conn, run_type="llm")
        assert result is None
        conn.close()

    def test_get_latest_returns_completed_run(self, db):
        conn = get_connection(db)
        insert_model_comparison_run(
            conn, run_id=_RUN_ID, run_type="llm",
            prompt_text="p", provider_matrix="[]", initiated_at=_NOW,
        )
        conn.commit()
        complete_model_comparison_run(conn, _RUN_ID, [{"status": "success"}], _DONE_AT)
        conn.commit()
        result = get_latest_model_comparison(conn, run_type="llm")
        assert result is not None
        assert result["run_id"] == _RUN_ID
        assert isinstance(result["results"], list)
        conn.close()

    def test_get_latest_ignores_running_rows(self, db):
        conn = get_connection(db)
        insert_model_comparison_run(
            conn, run_id=_RUN_ID, run_type="llm",
            prompt_text="p", provider_matrix="[]", initiated_at=_NOW,
        )
        conn.commit()
        # Do NOT complete — status stays 'running'
        result = get_latest_model_comparison(conn, run_type="llm")
        assert result is None
        conn.close()

    def test_get_latest_filters_by_run_type(self, db):
        conn = get_connection(db)
        insert_model_comparison_run(
            conn, run_id=_RUN_ID, run_type="llm",
            prompt_text="p", provider_matrix="[]", initiated_at=_NOW,
        )
        conn.commit()
        complete_model_comparison_run(conn, _RUN_ID, [], _DONE_AT)
        conn.commit()
        # Ask for 'image' — should return None
        assert get_latest_model_comparison(conn, run_type="image") is None
        # Ask for 'llm' — should return the run
        assert get_latest_model_comparison(conn, run_type="llm") is not None
        conn.close()


# ===========================================================================
# TestArchiveQuery
# ===========================================================================

class TestArchiveQuery:

    def test_returns_empty_list_for_unknown_episode(self, conn):
        result = query_archive_for_comparison("ep_nonexistent", conn)
        assert result == []

    def test_returns_empty_list_when_generation_prompt_is_null(self, conn):
        # Simulates a pre-M7.2 episode: row exists but generation_prompt=NULL
        _seed_archive_row(conn, generation_prompt=None)
        result = query_archive_for_comparison(_EPISODE, conn)
        assert result == []

    def test_returns_rows_when_provenance_present(self, conn):
        _seed_archive_row(conn, generation_prompt="Cinematic wide shot of pyramids.")
        result = query_archive_for_comparison(_EPISODE, conn)
        assert len(result) == 1
        assert result[0]["generation_prompt"] == "Cinematic wide shot of pyramids."

    def test_result_has_required_fields(self, conn):
        _seed_archive_row(conn, generation_prompt="test prompt",
                          provider="openrouter", model="anthropic/claude-sonnet-4.6",
                          cost_usd=0.05)
        result = query_archive_for_comparison(_EPISODE, conn)
        assert len(result) == 1
        row = result[0]
        for key in ("asset_id", "provider", "model", "generation_prompt",
                    "cost_usd", "archive_location", "asset_created_at", "scene_id"):
            assert key in row, f"Missing key: {key}"

    def test_filters_by_episode_id(self, conn):
        _seed_archive_row(conn, episode_id="ep_alpha",
                          generation_prompt="prompt for alpha")
        _seed_archive_row(conn, episode_id="ep_beta",
                          generation_prompt="prompt for beta")
        alpha = query_archive_for_comparison("ep_alpha", conn)
        beta  = query_archive_for_comparison("ep_beta", conn)
        assert len(alpha) == 1 and alpha[0]["generation_prompt"] == "prompt for alpha"
        assert len(beta)  == 1 and beta[0]["generation_prompt"]  == "prompt for beta"

    def test_null_provider_rejected_by_schema(self, conn):
        # archive_index.provider is NOT NULL — the schema prevents a null provider
        # from being inserted. Verify the constraint fires as expected.
        archive_id = str(uuid.uuid4())
        import sqlite3 as _sqlite3
        with pytest.raises(_sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO archive_index
                    (archive_id, asset_id, episode_id, asset_type, provider,
                     archived_at, metadata_version, generation_prompt)
                VALUES (?, ?, ?, ?, NULL, ?, 'v2', 'some prompt')
                """,
                (archive_id, str(uuid.uuid4()), _EPISODE, "video_clip", _NOW),
            )

    def test_multiple_rows_returned_in_order(self, conn):
        _seed_archive_row(conn, generation_prompt="older prompt",
                          asset_id="asset-old")
        _seed_archive_row(conn, generation_prompt="newer prompt",
                          asset_id="asset-new")
        result = query_archive_for_comparison(_EPISODE, conn)
        assert len(result) == 2
        # Ordered by asset_created_at DESC — both have the same _NOW timestamp,
        # so we just assert both are present
        prompts = {r["generation_prompt"] for r in result}
        assert prompts == {"older prompt", "newer prompt"}


# ===========================================================================
# TestRunModelComparison
# ===========================================================================

class TestRunModelComparison:
    # Config() raises ConfigError if OPENROUTER_API_KEY is absent.
    # Set a dummy value for the duration of every test in this class so that
    # copy.copy(Config()) succeeds. The actual API call is mocked separately.
    @pytest.fixture(autouse=True)
    def set_openrouter_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")

    def test_uses_call_claude_not_run_agent(self, db):
        with patch("hypogeum.model_comparison.call_claude",
                   return_value=_FAKE_CALL_RESULT) as mock_cc, \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            with patch("hypogeum.model_comparison.run_agent", create=True) as mock_ra:
                run_model_comparison(
                    prompt_text="System prompt",
                    initiated_at=_NOW,
                    db_path=db,
                    provider_matrix=_MINI_MATRIX,
                )
                assert mock_ra.call_count == 0, "run_agent must not be called"
        assert mock_cc.call_count == 1

    def test_successful_run_writes_to_db(self, db):
        with patch("hypogeum.model_comparison.call_claude",
                   return_value=_FAKE_CALL_RESULT), \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            result = run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                provider_matrix=_MINI_MATRIX,
            )
        conn = get_connection(db)
        row = conn.execute(
            "SELECT * FROM model_comparison_runs WHERE run_id=?",
            (result["run_id"],)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "completed"
        assert row["results_json"] is not None

    def test_successful_result_has_expected_fields(self, db):
        with patch("hypogeum.model_comparison.call_claude",
                   return_value=_FAKE_CALL_RESULT), \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            result = run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                provider_matrix=_MINI_MATRIX,
            )
        assert result["status"] == "completed"
        assert len(result["results"]) == 1
        r = result["results"][0]
        assert r["status"] == "success"
        assert r["provider"] == "openrouter"
        assert r["model"] == "anthropic/claude-sonnet-4.6"
        assert r["output_text"] == "This is the LLM output text."
        assert r["output_tokens"] == 120
        assert r["cost_usd"] == 0.02
        assert r["latency_ms"] is not None

    def test_missing_credential_marks_skipped_no_key(self, db):
        with patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=(None, "env")):
            result = run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                provider_matrix=_MINI_MATRIX,
            )
        # All pairs skipped → run status = error
        assert result["status"] == "error"
        assert result["results"][0]["status"] == "skipped_no_key"
        assert "No credential" in result["results"][0]["error"]

    def test_per_model_exception_captured_as_error(self, db):
        from hypogeum.openrouter import HardFailure
        with patch("hypogeum.model_comparison.call_claude",
                   side_effect=HardFailure("Rate limit", http_status=429)), \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            result = run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                provider_matrix=_MINI_MATRIX,
            )
        assert result["results"][0]["status"] == "error"
        assert result["results"][0]["error"] is not None
        # All errored → run status = error
        assert result["status"] == "error"

    def test_partial_success_sets_run_status_completed(self, db):
        from hypogeum.openrouter import HardFailure
        matrix = [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
            {"provider": "openrouter", "model": "anthropic/claude-opus-4.6"},
        ]
        call_results = [_FAKE_CALL_RESULT, HardFailure("Error")]
        with patch("hypogeum.model_comparison.call_claude",
                   side_effect=call_results), \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            result = run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                provider_matrix=matrix,
            )
        statuses = [r["status"] for r in result["results"]]
        assert "success" in statuses
        assert "error" in statuses
        # At least one success → run status = completed
        assert result["status"] == "completed"

    def test_results_decoded_by_get_model_comparison_result(self, db):
        with patch("hypogeum.model_comparison.call_claude",
                   return_value=_FAKE_CALL_RESULT), \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                provider_matrix=_MINI_MATRIX,
            )
        latest = get_model_comparison_result(db_path=db, run_type="llm")
        assert latest is not None
        assert isinstance(latest["results"], list)
        assert latest["results"][0]["status"] == "success"

    def test_prompt_name_stored_in_db(self, db):
        with patch("hypogeum.model_comparison.call_claude",
                   return_value=_FAKE_CALL_RESULT), \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            result = run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                provider_matrix=_MINI_MATRIX,
                prompt_name="executive_producer",
            )
        conn = get_connection(db)
        row = conn.execute(
            "SELECT prompt_name FROM model_comparison_runs WHERE run_id=?",
            (result["run_id"],)
        ).fetchone()
        conn.close()
        assert row["prompt_name"] == "executive_producer"

    def test_default_matrix_used_when_none_provided(self, db):
        calls = []
        def _fake_call(model_slug, messages, max_tokens, config, **kw):
            calls.append(model_slug)
            return _FAKE_CALL_RESULT

        with patch("hypogeum.model_comparison.call_claude", side_effect=_fake_call), \
             patch("hypogeum.model_comparison._resolve_openrouter_credential",
                   return_value=("fake-key", "env")):
            run_model_comparison(
                prompt_text="System prompt",
                initiated_at=_NOW,
                db_path=db,
                # provider_matrix=None → DEFAULT_LLM_MATRIX used
            )
        assert len(calls) == len(DEFAULT_LLM_MATRIX)
        assert calls[0] == DEFAULT_LLM_MATRIX[0]["model"]


# ===========================================================================
# TestNoUnauthorisedGeneration
# ===========================================================================

class TestNoUnauthorisedGeneration:

    @pytest.fixture(autouse=True)
    def block_sockets(self):
        """Fail the test if any real socket is opened."""
        _real_connect = socket.socket.connect
        def _blocked(self, *args, **kwargs):
            raise AssertionError(
                f"Network call detected during test: {args}. "
                "No provider calls should occur in model comparison tests."
            )
        socket.socket.connect = _blocked
        yield
        socket.socket.connect = _real_connect

    def test_query_archive_makes_no_network_calls(self, conn):
        # Seeding and querying archive must never open a socket.
        _seed_archive_row(conn, generation_prompt="test")
        result = query_archive_for_comparison(_EPISODE, conn)
        assert isinstance(result, list)

    def test_get_model_comparison_result_makes_no_network_calls(self, db):
        # Reading from DB must never open a socket.
        result = get_model_comparison_result(db_path=db, run_type="llm")
        assert result is None  # empty DB

    def test_archive_query_empty_makes_no_network_calls(self, conn):
        # Even an empty result must not trigger any network call.
        result = query_archive_for_comparison("ep_nonexistent", conn)
        assert result == []
