"""
tests/test_compare_dashboard.py — Targeted tests for the model comparison dashboard.

Coverage:
    TestComparePage      — GET /compare HTML surface
    TestCompareRunAPI    — POST /api/compare/run
    TestCompareResultsAPI — GET /api/compare/results
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from hypogeum.db import initialize_schema


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "cmp_dash_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


_FAKE_RUN = {
    "run_id":       "cccc-0000-0000-0000-0001",
    "run_type":     "llm",
    "prompt_name":  "executive_producer",
    "status":       "completed",
    "initiated_at": "2026-06-26T10:00:00+00:00",
    "completed_at": "2026-06-26T10:01:30+00:00",
    "results": [
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "status": "success",
            "latency_ms": 1200,
            "cost_usd": 0.02,
            "output_text": "This is the comparison output.",
            "output_tokens": 120,
            "error": None,
            "credential_source": "env",
            "archive_asset_id": None,
        }
    ],
}


# ===========================================================================
# TestComparePage
# ===========================================================================

class TestComparePage:

    def test_compare_page_returns_200(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert resp.status_code == 200

    def test_compare_page_is_html(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"<!DOCTYPE html>" in resp.data

    def test_compare_page_shows_heading(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"Model Output Comparison" in resp.data

    def test_compare_page_shows_no_run_message_when_empty(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"No comparison run yet" in resp.data

    def test_compare_page_shows_prompt_dropdown(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"sel-prompt" in resp.data

    def test_compare_page_shows_model_matrix(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"matrix-cb" in resp.data
        assert b"claude-sonnet-4.6" in resp.data

    def test_compare_page_shows_run_button(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"Run Comparison" in resp.data

    def test_compare_page_shows_modal_js(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"confirmRun" in resp.data
        assert b"openModal" in resp.data

    def test_compare_page_shows_api_endpoint(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"/api/compare/run" in resp.data

    def test_compare_page_shows_results_when_run_exists(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=_FAKE_RUN):
            resp = client.get("/compare")
        assert b"executive_producer" in resp.data
        assert b"This is the comparison output." in resp.data

    def test_compare_page_shows_last_run_timestamp(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=_FAKE_RUN):
            resp = client.get("/compare")
        assert b"Last run" in resp.data or b"2026-06-26" in resp.data

    def test_compare_page_shows_archive_tab(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b"Archive" in resp.data

    def test_compare_page_nav_contains_link(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/compare")
        assert b'href="/compare"' in resp.data

    def test_compare_page_archive_empty_message(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None), \
             patch("run_dashboard.query_archive_for_comparison", return_value=[]):
            resp = client.get("/compare?episode_id=ep_val_01")
        assert b"No provenance data" in resp.data

    def test_compare_page_archive_shows_rows(self, client):
        fake_rows = [{
            "scene_id": "sc_001",
            "model": "anthropic/claude-sonnet-4.6",
            "cost_usd": 0.02,
            "provider": "openrouter",
            "asset_created_at": "2026-06-26T10:00:00",
            "generation_prompt": "test prompt",
        }]
        with patch("run_dashboard.get_model_comparison_result", return_value=None), \
             patch("run_dashboard.query_archive_for_comparison", return_value=fake_rows):
            resp = client.get("/compare?episode_id=ep_post_m72")
        assert b"sc_001" in resp.data


# ===========================================================================
# TestCompareRunAPI
# ===========================================================================

class TestCompareRunAPI:

    def test_run_returns_200_on_success(self, client):
        with patch("run_dashboard.get_prompt") as mock_gp, \
             patch("run_dashboard.run_model_comparison", return_value=_FAKE_RUN):
            mock_gp.return_value = MagicMock(body="System prompt text", name="executive_producer")
            resp = client.post(
                "/api/compare/run",
                json={"prompt_name": "executive_producer"},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["status"] == "completed"

    def test_run_returns_400_when_prompt_name_missing(self, client):
        resp = client.post(
            "/api/compare/run",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_run_returns_400_when_prompt_not_found(self, client):
        with patch("run_dashboard.get_prompt", return_value=None):
            resp = client.post(
                "/api/compare/run",
                json={"prompt_name": "nonexistent_prompt"},
                content_type="application/json",
            )
        assert resp.status_code == 400
        assert "not found" in resp.get_json()["error"]

    def test_run_returns_500_on_engine_exception(self, client):
        with patch("run_dashboard.get_prompt") as mock_gp, \
             patch("run_dashboard.run_model_comparison",
                   side_effect=RuntimeError("engine failure")):
            mock_gp.return_value = MagicMock(body="prompt", name="executive_producer")
            resp = client.post(
                "/api/compare/run",
                json={"prompt_name": "executive_producer"},
                content_type="application/json",
            )
        assert resp.status_code == 500
        assert resp.get_json()["ok"] is False

    def test_run_passes_provider_matrix_to_engine(self, client):
        calls = []
        def _fake_run(**kwargs):
            calls.append(kwargs)
            return _FAKE_RUN

        custom_matrix = [{"provider": "openrouter", "model": "openai/gpt-5.5-pro"}]
        with patch("run_dashboard.get_prompt") as mock_gp, \
             patch("run_dashboard.run_model_comparison", side_effect=_fake_run):
            mock_gp.return_value = MagicMock(body="System prompt", name="executive_producer")
            client.post(
                "/api/compare/run",
                json={"prompt_name": "executive_producer",
                      "provider_matrix": custom_matrix},
                content_type="application/json",
            )
        assert calls[0]["provider_matrix"] == custom_matrix

    def test_run_response_includes_results(self, client):
        with patch("run_dashboard.get_prompt") as mock_gp, \
             patch("run_dashboard.run_model_comparison", return_value=_FAKE_RUN):
            mock_gp.return_value = MagicMock(body="System prompt", name="executive_producer")
            resp = client.post(
                "/api/compare/run",
                json={"prompt_name": "executive_producer"},
                content_type="application/json",
            )
        data = resp.get_json()
        assert isinstance(data.get("results"), list)
        assert len(data["results"]) == 1


# ===========================================================================
# TestCompareResultsAPI
# ===========================================================================

class TestCompareResultsAPI:

    def test_results_returns_200(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/api/compare/results")
        assert resp.status_code == 200

    def test_results_returns_null_when_empty(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=None):
            resp = client.get("/api/compare/results")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["run"] is None

    def test_results_returns_run_when_exists(self, client):
        with patch("run_dashboard.get_model_comparison_result", return_value=_FAKE_RUN):
            resp = client.get("/api/compare/results")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["run"]["run_id"] == _FAKE_RUN["run_id"]

    def test_results_run_type_param_forwarded(self, client):
        captured = []
        def _fake_get(**kwargs):
            captured.append(kwargs)
            return None

        with patch("run_dashboard.get_model_comparison_result", side_effect=_fake_get):
            client.get("/api/compare/results?run_type=image")
        assert captured[0].get("run_type") == "image"

    def test_results_returns_500_on_error(self, client):
        with patch("run_dashboard.get_model_comparison_result",
                   side_effect=RuntimeError("db error")):
            resp = client.get("/api/compare/results")
        assert resp.status_code == 500
        assert resp.get_json()["ok"] is False
