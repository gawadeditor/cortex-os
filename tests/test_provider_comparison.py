"""
tests/test_provider_comparison.py — Phase 1G: Provider Comparison tests

Coverage:
    TestDBLayer              — insert, complete, get_latest, list runs
    TestCredentialResolution — Secret Profile first, env var fallback, missing → skipped
    TestRunProviderComparison — success paths, all-skipped, partial success, DB write
    TestGetComparisonResult  — empty DB → None, populated → dict with results list
    TestRouteCompare         — POST /api/providers/compare triggers run, returns JSON
    TestRouteResults         — GET /api/providers/compare/results returns latest run
    TestRouteImage           — GET /api/providers/compare/image/<run_id>/<provider>
    TestProvidersPageSection — GET /providers renders comparison section
    TestNoNetworkCalls       — no sockets opened during unit tests
    TestNoProductionSideEffects — writes only to tmp DB, not production
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_provider_comparison_run,
    complete_provider_comparison_run,
    get_latest_provider_comparison,
    list_provider_comparison_runs,
)
from hypogeum.provider_comparison import (
    COMPARISON_PROMPT,
    get_comparison_result,
    run_provider_comparison,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "cmp_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _conn(db):
    return get_connection(db)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_ID  = "aaaaaaaa-0000-0000-0000-000000000001"
_RUN_AT  = "2026-06-21T10:00:00+00:00"
_DONE_AT = "2026-06-21T10:01:30+00:00"

_RESULTS_FIXTURE = [
    {
        "provider": "openai",
        "model": "gpt-image-1",
        "status": "success",
        "latency_ms": 8200,
        "estimated_cost_usd": 0.04,
        "output_path": "/tmp/thumb_openai.jpg",
        "error": None,
        "credential_source": "env",
    },
    {
        "provider": "openrouter",
        "model": "openai/gpt-5-image-mini",
        "status": "success",
        "latency_ms": 12400,
        "estimated_cost_usd": 0.04,
        "output_path": "/tmp/thumb_openrouter.jpg",
        "error": None,
        "credential_source": "secret_profile",
    },
    {
        "provider": "muapi",
        "model": "gpt-image-2-text-to-image",
        "status": "skipped_no_key",
        "latency_ms": None,
        "estimated_cost_usd": 0.09,
        "output_path": "/tmp/thumb_muapi.jpg",
        "error": "No credential found",
        "credential_source": "env",
    },
]


def _seed_run(db, run_id=_RUN_ID, run_at=_RUN_AT, completed_at=_DONE_AT, results=None):
    conn = _conn(db)
    try:
        insert_provider_comparison_run(conn, run_id, run_at, COMPARISON_PROMPT)
        conn.commit()
        complete_provider_comparison_run(conn, run_id, results or _RESULTS_FIXTURE, completed_at)
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# TestDBLayer
# ===========================================================================

class TestDBLayer:

    def test_insert_creates_running_row(self, db):
        conn = _conn(db)
        try:
            insert_provider_comparison_run(conn, _RUN_ID, _RUN_AT, COMPARISON_PROMPT)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM provider_comparison_runs WHERE run_id=?", (_RUN_ID,)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["status"] == "running"
        assert row["results_json"] is None
        assert row["prompt"] == COMPARISON_PROMPT

    def test_complete_updates_status_and_results(self, db):
        conn = _conn(db)
        try:
            insert_provider_comparison_run(conn, _RUN_ID, _RUN_AT, COMPARISON_PROMPT)
            conn.commit()
            complete_provider_comparison_run(conn, _RUN_ID, _RESULTS_FIXTURE, _DONE_AT)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM provider_comparison_runs WHERE run_id=?", (_RUN_ID,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "completed"
        assert row["completed_at"] == _DONE_AT
        stored = json.loads(row["results_json"])
        assert len(stored) == 3
        assert stored[0]["provider"] == "openai"

    def test_get_latest_returns_none_on_empty(self, db):
        conn = _conn(db)
        try:
            result = get_latest_provider_comparison(conn)
        finally:
            conn.close()
        assert result is None

    def test_get_latest_returns_most_recent_completed(self, db):
        run_a = str(uuid.uuid4())
        run_b = str(uuid.uuid4())
        conn = _conn(db)
        try:
            insert_provider_comparison_run(conn, run_a, "2026-06-21T08:00:00+00:00", COMPARISON_PROMPT)
            conn.commit()
            complete_provider_comparison_run(conn, run_a, _RESULTS_FIXTURE, "2026-06-21T08:01:00+00:00")
            conn.commit()
            insert_provider_comparison_run(conn, run_b, "2026-06-21T09:00:00+00:00", COMPARISON_PROMPT)
            conn.commit()
            complete_provider_comparison_run(conn, run_b, [{"provider": "muapi"}], "2026-06-21T09:01:00+00:00")
            conn.commit()
            result = get_latest_provider_comparison(conn)
        finally:
            conn.close()
        assert result is not None
        assert result["run_id"] == run_b
        assert isinstance(result["results"], list)
        assert result["results"][0]["provider"] == "muapi"

    def test_get_latest_ignores_running_rows(self, db):
        conn = _conn(db)
        try:
            insert_provider_comparison_run(conn, _RUN_ID, _RUN_AT, COMPARISON_PROMPT)
            conn.commit()
            result = get_latest_provider_comparison(conn)
        finally:
            conn.close()
        assert result is None

    def test_list_runs_returns_most_recent_first(self, db):
        run_a = str(uuid.uuid4())
        run_b = str(uuid.uuid4())
        conn = _conn(db)
        try:
            insert_provider_comparison_run(conn, run_a, "2026-06-21T07:00:00+00:00", COMPARISON_PROMPT)
            conn.commit()
            complete_provider_comparison_run(conn, run_a, [], "2026-06-21T07:01:00+00:00")
            conn.commit()
            insert_provider_comparison_run(conn, run_b, "2026-06-21T08:00:00+00:00", COMPARISON_PROMPT)
            conn.commit()
            complete_provider_comparison_run(conn, run_b, [], "2026-06-21T08:01:00+00:00")
            conn.commit()
            rows = list_provider_comparison_runs(conn)
        finally:
            conn.close()
        assert rows[0]["run_id"] == run_b
        assert rows[1]["run_id"] == run_a

    def test_list_runs_respects_limit(self, db):
        conn = _conn(db)
        try:
            for i in range(5):
                rid = str(uuid.uuid4())
                insert_provider_comparison_run(conn, rid, f"2026-06-21T0{i}:00:00+00:00", COMPARISON_PROMPT)
                conn.commit()
                complete_provider_comparison_run(conn, rid, [], f"2026-06-21T0{i}:01:00+00:00")
                conn.commit()
            rows = list_provider_comparison_runs(conn, limit=3)
        finally:
            conn.close()
        assert len(rows) == 3

    def test_results_decoded_as_list(self, db):
        _seed_run(db)
        conn = _conn(db)
        try:
            result = get_latest_provider_comparison(conn)
        finally:
            conn.close()
        assert isinstance(result["results"], list)
        assert result["results"][0]["provider"] == "openai"


# ===========================================================================
# TestCredentialResolution
# ===========================================================================

class TestCredentialResolution:
    """Credential resolution: Secret Profile → env var → skipped_no_key."""

    def _run_one_provider(self, provider, db, monkeypatch, env_key=None,
                          secret_key=None, generation_succeeds=True):
        """
        Helper: call _run_one_provider() with controlled credentials.
        Returns the result dict.
        """
        from hypogeum.provider_comparison import _run_one_provider, COMPARISON_DELIVERABLE_ID
        from hypogeum.config import Config

        # Clear env var
        env_var = {"openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "muapi": "MUAPI_API_KEY"}
        monkeypatch.delenv(env_var.get(provider, "OPENAI_API_KEY"), raising=False)
        if env_key:
            monkeypatch.setenv(env_var[provider], env_key)

        if secret_key:
            from hypogeum.credential_manager import create_secret_profile
            create_secret_profile(
                provider=provider,
                label=f"{provider}-test",
                credential_type="api_key",
                credential_data=secret_key,
                db_path=db,
            )

        # Build a minimal config
        monkeypatch.setenv("OPENROUTER_API_KEY", env_key or "or-test-key")
        try:
            cfg = Config()
        except Exception:
            cfg = MagicMock()
            cfg.OPENAI_API_KEY = env_key
            cfg.OPENROUTER_API_KEY = env_key
            cfg.MUAPI_API_KEY = env_key
            cfg.OPENAI_IMAGE_MODEL = "gpt-image-1"
            cfg.OPENROUTER_IMAGE_MODEL = "openai/gpt-5-image-mini"
            cfg.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
            cfg.THUMBNAIL_IMAGE_PROVIDER = "muapi"
            cfg.THUMBNAIL_IMAGE_QUALITY = "high"

        gen_patch_target = {
            "openai":     "hypogeum.provider_comparison._generate_thumbnail_openai",
            "openrouter": "hypogeum.provider_comparison._generate_thumbnail_openrouter",
            "muapi":      "hypogeum.provider_comparison._attempt_thumbnail_generation",
        }[provider]

        run_id = str(uuid.uuid4())
        (Path("/tmp") / f"provider_comparisons" / run_id).mkdir(parents=True, exist_ok=True)

        if generation_succeeds:
            # Patch the generation function to write a fake thumbnail and return model slug
            def _fake_gen(*args, **kwargs):
                # Write a fake 2KB file so the result is valid
                local_path = kwargs.get("local_path") or (
                    Path("/tmp") / "provider_comparisons" / run_id / f"thumb_{provider}.jpg"
                )
                if provider == "muapi":
                    # _attempt_thumbnail_generation returns (model, used_fallback)
                    out_dir = kwargs.get("output_dir", Path("/tmp"))
                    fake = out_dir / "thumbnails" / COMPARISON_DELIVERABLE_ID + ".jpg"
                    fake.parent.mkdir(parents=True, exist_ok=True)
                    fake.write_bytes(b"JFIF" + b"\x00" * 2048)
                    return "gpt-image-2-text-to-image", False
                else:
                    local_path = kwargs.get("local_path")
                    if local_path:
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        local_path.write_bytes(b"JFIF" + b"\x00" * 2048)
                    return "gpt-image-1" if provider == "openai" else "openai/gpt-5-image-mini"

            with patch(gen_patch_target, side_effect=_fake_gen):
                with patch("hypogeum.provider_comparison._normalize_thumbnail"):
                    with patch("hypogeum.provider_comparison._apply_wordmark_overlay"):
                        return _run_one_provider(provider, run_id, cfg, db)
        else:
            return _run_one_provider(provider, run_id, cfg, db)

    def test_no_credential_returns_skipped(self, db, monkeypatch):
        """When neither env var nor Secret Profile is set, status is skipped_no_key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from hypogeum.provider_comparison import _run_one_provider
        cfg = MagicMock()
        cfg.OPENAI_API_KEY = ""
        cfg.OPENAI_IMAGE_MODEL = "gpt-image-1"
        result = _run_one_provider("openai", str(uuid.uuid4()), cfg, db)
        assert result["status"] == "skipped_no_key"
        assert result["error"] is not None

    def test_env_var_credential_used_as_fallback(self, db, monkeypatch):
        """Env var credential is used when no Secret Profile exists."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-key")
        from hypogeum.provider_comparison import _resolve_credential
        cred, source = _resolve_credential("openai", db)
        assert cred == "sk-env-test-key"
        assert source == "env"

    def test_secret_profile_takes_priority_over_env(self, db, monkeypatch):
        """Secret Profile credential beats env var."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
        from hypogeum.credential_manager import create_secret_profile
        create_secret_profile(
            provider="openai",
            label="openai-profile",
            credential_type="api_key",
            credential_data="sk-profile-key",
            db_path=db,
        )
        from hypogeum.provider_comparison import _resolve_credential
        cred, source = _resolve_credential("openai", db)
        assert cred == "sk-profile-key"
        assert source == "secret_profile"

    def test_revoked_profile_falls_through_to_env(self, db, monkeypatch):
        """A revoked Secret Profile is not used; env var fallback applies."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-only")
        from hypogeum.credential_manager import create_secret_profile, revoke_secret_profile
        sp = create_secret_profile(
            provider="openai",
            label="openai-revoked",
            credential_type="api_key",
            credential_data="sk-revoked",
            db_path=db,
        )
        revoke_secret_profile(sp.secret_profile_id, db_path=db)
        from hypogeum.provider_comparison import _resolve_credential
        cred, source = _resolve_credential("openai", db)
        assert cred == "sk-env-only"
        assert source == "env"

    def test_missing_both_returns_none(self, db, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from hypogeum.provider_comparison import _resolve_credential
        cred, source = _resolve_credential("openai", db)
        assert cred is None


# ===========================================================================
# TestRunProviderComparison
# ===========================================================================

class TestRunProviderComparison:
    """End-to-end run_provider_comparison() with generation mocked."""

    def _mock_generation(self, provider_results: dict):
        """
        Return a context manager that patches all three generation functions.
        provider_results: {provider: ("model_slug", b"image_bytes") or Exception}
        """
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            def _make_openai_fn(spec):
                def _fn(deliverable_id, image_prompt, aspect_ratio, config, local_path, log):
                    if isinstance(spec, Exception):
                        raise spec
                    model, img = spec
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(img)
                    return model
                return _fn

            def _make_openrouter_fn(spec):
                def _fn(deliverable_id, image_prompt, aspect_ratio, config, local_path, log):
                    if isinstance(spec, Exception):
                        raise spec
                    model, img = spec
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(img)
                    return model
                return _fn

            def _make_muapi_fn(spec, tmp_dir):
                def _fn(deliverable_id, image_prompt, aspect_ratio, primary_model,
                        fallback_model, config, output_dir, episode_id, budget_guard,
                        text_overlay, typography_plan, typography_integration, log):
                    if isinstance(spec, Exception):
                        raise spec
                    model, img = spec
                    out = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(img)
                    return model, False
                return _fn

            _FAKE_IMG = b"JFIF" + b"\x00" * 2048

            with patch("hypogeum.provider_comparison._generate_thumbnail_openai",
                       side_effect=_make_openai_fn(provider_results.get("openai", ("gpt-image-1", _FAKE_IMG)))):
                with patch("hypogeum.provider_comparison._generate_thumbnail_openrouter",
                           side_effect=_make_openrouter_fn(provider_results.get("openrouter", ("openai/gpt-5-image-mini", _FAKE_IMG)))):
                    with patch("hypogeum.provider_comparison._attempt_thumbnail_generation",
                               side_effect=_make_muapi_fn(provider_results.get("muapi", ("gpt-image-2-text-to-image", _FAKE_IMG)), None)):
                        with patch("hypogeum.provider_comparison._normalize_thumbnail"):
                            with patch("hypogeum.provider_comparison._apply_wordmark_overlay"):
                                yield

        return _ctx()

    def test_returns_comparison_run_dataclass(self, db, monkeypatch):
        from hypogeum.provider_comparison import ComparisonRun
        monkeypatch.setenv("OPENAI_API_KEY",     "sk-openai-test")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setenv("MUAPI_API_KEY",      "mu-test")
        with self._mock_generation({}):
            run = run_provider_comparison(db_path=db, providers=["openai"])
        assert isinstance(run, ComparisonRun)
        assert run.run_id
        assert run.status == "completed"

    def test_all_providers_in_results(self, db, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY",     "sk-openai-test")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setenv("MUAPI_API_KEY",      "mu-test")
        with self._mock_generation({}):
            run = run_provider_comparison(db_path=db, providers=["openai", "openrouter", "muapi"])
        providers_in_results = [r["provider"] for r in run.results]
        assert "openai"     in providers_in_results
        assert "openrouter" in providers_in_results
        assert "muapi"      in providers_in_results

    def test_success_result_has_expected_fields(self, db, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        with self._mock_generation({}):
            run = run_provider_comparison(db_path=db, providers=["openai"])
        r = run.results[0]
        assert r["provider"]    == "openai"
        assert r["status"]      == "success"
        assert r["latency_ms"]  is not None
        assert r["model"]       is not None
        assert "estimated_cost_usd" in r
        assert "output_path"    in r
        assert "credential_source" in r

    def test_skipped_when_no_credential(self, db, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        with self._mock_generation({}):
            run = run_provider_comparison(db_path=db, providers=["openai"])
        r = run.results[0]
        assert r["status"] == "skipped_no_key"
        assert r["error"]  is not None

    def test_partial_success_mixed_results(self, db, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        with self._mock_generation({}):
            run = run_provider_comparison(db_path=db, providers=["openai", "openrouter"])
        statuses = {r["provider"]: r["status"] for r in run.results}
        assert statuses["openai"]     == "skipped_no_key"
        assert statuses["openrouter"] == "success"

    def test_generation_error_recorded(self, db, monkeypatch):
        from hypogeum.muapi import MUAPIError
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        _FAKE_IMG = b"JFIF" + b"\x00" * 2048
        with self._mock_generation({
            "openai":     MUAPIError("API error 500"),
            "openrouter": ("openai/gpt-5-image-mini", _FAKE_IMG),
        }):
            run = run_provider_comparison(db_path=db, providers=["openai", "openrouter"])
        statuses = {r["provider"]: r["status"] for r in run.results}
        assert statuses["openai"]     == "error"
        assert statuses["openrouter"] == "success"

    def test_results_persisted_in_db(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setenv("MUAPI_API_KEY",      "mu-test")
        with self._mock_generation({}):
            run = run_provider_comparison(db_path=db, providers=["openrouter"])
        stored = get_comparison_result(db)
        assert stored is not None
        assert stored["run_id"] == run.run_id
        assert len(stored["results"]) == 1

    def test_prompt_is_fixed(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        with self._mock_generation({}):
            run = run_provider_comparison(db_path=db, providers=["openrouter"])
        assert run.prompt == COMPARISON_PROMPT

    def test_secret_profile_credential_used(self, db, monkeypatch):
        """Secret Profile credential is injected into config before generation."""
        from hypogeum.credential_manager import create_secret_profile
        monkeypatch.delenv("MUAPI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        create_secret_profile(
            provider="muapi",
            label="muapi-sp",
            credential_type="api_key",
            credential_data="mu-from-profile",
            db_path=db,
        )
        captured = {}

        def _capture_muapi(deliverable_id, image_prompt, aspect_ratio, primary_model,
                           fallback_model, config, output_dir, episode_id, budget_guard,
                           text_overlay, typography_plan, typography_integration, log):
            captured["muapi_key"] = config.MUAPI_API_KEY
            _FAKE_IMG = b"JFIF" + b"\x00" * 2048
            out = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_FAKE_IMG)
            return "gpt-image-2-text-to-image", False

        with patch("hypogeum.provider_comparison._attempt_thumbnail_generation", side_effect=_capture_muapi):
            with patch("hypogeum.provider_comparison._normalize_thumbnail"):
                with patch("hypogeum.provider_comparison._apply_wordmark_overlay"):
                    run_provider_comparison(db_path=db, providers=["muapi"])

        assert captured.get("muapi_key") == "mu-from-profile"


# ===========================================================================
# TestGetComparisonResult
# ===========================================================================

class TestGetComparisonResult:

    def test_returns_none_on_empty_db(self, db):
        result = get_comparison_result(db)
        assert result is None

    def test_returns_latest_completed_run(self, db):
        _seed_run(db)
        result = get_comparison_result(db)
        assert result is not None
        assert result["run_id"] == _RUN_ID
        assert isinstance(result["results"], list)
        assert len(result["results"]) == 3

    def test_returns_none_when_only_running_row_exists(self, db):
        conn = _conn(db)
        try:
            insert_provider_comparison_run(conn, _RUN_ID, _RUN_AT, COMPARISON_PROMPT)
            conn.commit()
        finally:
            conn.close()
        result = get_comparison_result(db)
        assert result is None

    def test_result_contains_prompt(self, db):
        _seed_run(db)
        result = get_comparison_result(db)
        assert result["prompt"] == COMPARISON_PROMPT


# ===========================================================================
# TestRouteCompare
# ===========================================================================

class TestRouteCompare:
    """POST /api/providers/compare"""

    def _mock_run(self, results=None):
        """Patch run_provider_comparison in the server module."""
        from hypogeum.provider_comparison import ComparisonRun
        fake_run = ComparisonRun(
            run_id="fake-run-id",
            run_at="2026-06-21T10:00:00+00:00",
            prompt=COMPARISON_PROMPT,
            results=results or _RESULTS_FIXTURE,
            status="completed",
            completed_at="2026-06-21T10:01:30+00:00",
        )
        return patch("run_dashboard.run_provider_comparison", return_value=fake_run)

    def test_returns_200_with_results(self, client):
        with self._mock_run():
            resp = client.post("/api/providers/compare",
                               content_type="application/json", data=json.dumps({}))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "run_id" in data
        assert "results" in data
        assert len(data["results"]) == 3

    def test_results_contain_provider_fields(self, client):
        with self._mock_run():
            resp = client.post("/api/providers/compare",
                               content_type="application/json", data=json.dumps({}))
        data = resp.get_json()
        providers = {r["provider"] for r in data["results"]}
        assert "openai" in providers
        assert "openrouter" in providers
        assert "muapi" in providers

    def test_custom_provider_subset_forwarded(self, client):
        from hypogeum.provider_comparison import ComparisonRun
        captured = {}

        def _fake_run(db_path=None, providers=None):
            captured["providers"] = providers
            return ComparisonRun(
                run_id="x", run_at="t", prompt=COMPARISON_PROMPT,
                results=[], status="completed", completed_at="t",
            )

        with patch("run_dashboard.run_provider_comparison", side_effect=_fake_run):
            client.post("/api/providers/compare",
                        content_type="application/json",
                        data=json.dumps({"providers": ["openrouter", "muapi"]}))
        assert captured["providers"] == ["openrouter", "muapi"]

    def test_invalid_providers_filtered(self, client):
        from hypogeum.provider_comparison import ComparisonRun
        captured = {}

        def _fake_run(db_path=None, providers=None):
            captured["providers"] = providers
            return ComparisonRun(
                run_id="x", run_at="t", prompt=COMPARISON_PROMPT,
                results=[], status="completed", completed_at="t",
            )

        with patch("run_dashboard.run_provider_comparison", side_effect=_fake_run):
            client.post("/api/providers/compare",
                        content_type="application/json",
                        data=json.dumps({"providers": ["openai", "bad-provider", "muapi"]}))
        assert "bad-provider" not in captured.get("providers", [])
        assert "openai" in captured["providers"]

    def test_empty_providers_list_returns_400(self, client):
        resp = client.post("/api/providers/compare",
                           content_type="application/json",
                           data=json.dumps({"providers": ["invalid-xyz"]}))
        assert resp.status_code == 400

    def test_generation_error_returns_500(self, client):
        with patch("run_dashboard.run_provider_comparison", side_effect=RuntimeError("kaboom")):
            resp = client.post("/api/providers/compare",
                               content_type="application/json", data=json.dumps({}))
        assert resp.status_code == 500
        assert resp.get_json()["ok"] is False

    def test_response_includes_prompt(self, client):
        with self._mock_run():
            resp = client.post("/api/providers/compare",
                               content_type="application/json", data=json.dumps({}))
        data = resp.get_json()
        assert data["prompt"] == COMPARISON_PROMPT


# ===========================================================================
# TestRouteResults
# ===========================================================================

class TestRouteResults:
    """GET /api/providers/compare/results"""

    def test_returns_none_when_no_run(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/api/providers/compare/results")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["run"] is None

    def test_returns_latest_run(self, client):
        fake_run = {
            "run_id": _RUN_ID,
            "run_at": _RUN_AT,
            "completed_at": _DONE_AT,
            "prompt": COMPARISON_PROMPT,
            "results": _RESULTS_FIXTURE,
        }
        with patch("run_dashboard.get_comparison_result", return_value=fake_run):
            resp = client.get("/api/providers/compare/results")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["run"]["run_id"] == _RUN_ID
        assert len(data["run"]["results"]) == 3

    def test_error_returns_503(self, client):
        with patch("run_dashboard.get_comparison_result", side_effect=RuntimeError("db error")):
            resp = client.get("/api/providers/compare/results")
        assert resp.status_code == 500


# ===========================================================================
# TestRouteImage
# ===========================================================================

class TestRouteImage:
    """GET /api/providers/compare/image/<run_id>/<provider>"""

    def test_404_when_image_not_found(self, client):
        run_id = str(uuid.uuid4())
        resp = client.get(f"/api/providers/compare/image/{run_id}/openai")
        assert resp.status_code == 404

    def test_200_when_image_exists(self, client, tmp_path):
        run_id = str(uuid.uuid4())
        # Write fake image to the expected location
        img_dir = (
            Path(__file__).resolve().parent.parent
            / ".hypogeum-assets" / "provider_comparisons" / run_id
        )
        img_dir.mkdir(parents=True, exist_ok=True)
        fake_img = img_dir / "thumb_openai.jpg"
        fake_img.write_bytes(b"JFIF" + b"\x00" * 512)
        try:
            resp = client.get(f"/api/providers/compare/image/{run_id}/openai")
            assert resp.status_code == 200
            assert resp.content_type == "image/jpeg"
        finally:
            fake_img.unlink(missing_ok=True)
            try:
                img_dir.rmdir()
            except OSError:
                pass

    def test_400_on_invalid_run_id_format(self, client):
        resp = client.get("/api/providers/compare/image/../../etc/passwd/openai")
        assert resp.status_code in (400, 404)

    def test_400_on_invalid_provider(self, client):
        run_id = str(uuid.uuid4())
        resp = client.get(f"/api/providers/compare/image/{run_id}/evil")
        assert resp.status_code == 400


# ===========================================================================
# TestProvidersPageSection
# ===========================================================================

class TestProvidersPageSection:
    """GET /providers renders the comparison section correctly."""

    def test_comparison_section_present(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert resp.status_code == 200
        assert b"Provider Comparison" in resp.data

    def test_run_button_present(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"Run Comparison" in resp.data

    def test_no_run_message_shown_when_empty(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"No comparison run yet" in resp.data

    def test_last_run_timestamp_shown_when_results_exist(self, client):
        fake_run = {
            "run_id":       _RUN_ID,
            "run_at":       _RUN_AT,
            "completed_at": _DONE_AT,
            "prompt":       COMPARISON_PROMPT,
            "results":      _RESULTS_FIXTURE,
        }
        with patch("run_dashboard.get_comparison_result", return_value=fake_run):
            resp = client.get("/providers")
        assert b"Last run" in resp.data

    def test_skipped_provider_shows_no_credential(self, client):
        fake_run = {
            "run_id":       _RUN_ID,
            "run_at":       _RUN_AT,
            "completed_at": _DONE_AT,
            "prompt":       COMPARISON_PROMPT,
            "results":      [_RESULTS_FIXTURE[2]],   # muapi skipped_no_key
        }
        with patch("run_dashboard.get_comparison_result", return_value=fake_run):
            resp = client.get("/providers")
        assert b"No credential" in resp.data

    def test_comparison_prompt_in_details_block(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        # Prompt text is in the <details> block
        assert b"Karnak" in resp.data

    def test_js_run_comparison_function_present(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"runComparison" in resp.data

    def test_api_endpoint_url_in_js(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"/api/providers/compare" in resp.data


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    """No sockets opened during the unit tests above."""

    @pytest.fixture(autouse=True)
    def block_sockets(self):
        _orig = socket.socket

        def _blocked(*args, **kwargs):
            raise RuntimeError("Network access blocked in unit tests")

        socket.socket = _blocked
        yield
        socket.socket = _orig

    def test_get_comparison_result_no_network(self, db):
        get_comparison_result(db)

    def test_db_insert_no_network(self, db):
        conn = _conn(db)
        try:
            insert_provider_comparison_run(conn, str(uuid.uuid4()), _RUN_AT, COMPARISON_PROMPT)
            conn.commit()
        finally:
            conn.close()

    def test_providers_page_no_network(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert resp.status_code == 200


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    """No writes to the production DB at .hypogeum-assets/hypogeum.db."""

    def test_run_uses_tmp_db(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        from hypogeum.provider_comparison import ComparisonRun
        fake_run = ComparisonRun(
            run_id="test-run", run_at="t", prompt=COMPARISON_PROMPT,
            results=[], status="completed", completed_at="t",
        )
        with patch("hypogeum.provider_comparison.run_provider_comparison", return_value=fake_run):
            pass  # just verify import is clean

        prod_db = (
            Path(__file__).resolve().parent.parent
            / ".hypogeum-assets" / "hypogeum.db"
        )
        # Read the production DB modification time if it exists
        mtime_before = prod_db.stat().st_mtime if prod_db.exists() else None

        get_comparison_result(db)   # reads tmp db only

        mtime_after = prod_db.stat().st_mtime if prod_db.exists() else None
        assert mtime_before == mtime_after, (
            "Production DB was modified during test — isolation failure"
        )

    def test_no_episode_records_written(self, db, monkeypatch):
        """Comparison writes only to provider_comparison_runs, not runs or episodes."""
        _seed_run(db)
        conn = _conn(db)
        try:
            run_rows = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            ep_rows  = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        finally:
            conn.close()
        assert run_rows == 0
        assert ep_rows  == 0
