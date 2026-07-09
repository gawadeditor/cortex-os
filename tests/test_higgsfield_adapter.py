"""
tests/test_higgsfield_adapter.py — Higgsfield adapter unit tests

Coverage:
    Credential check — missing key/secret returns valid=False
    Credential check — both present returns request to API
    submit_dop_generation — dry_run returns dry_run result immediately
    submit_dop_generation — unknown model raises HiggsFieldError
    submit_dop_generation — 401 response raises HiggsFieldAuthError
    submit_dop_generation — 402 response raises HiggsFieldQuotaError
    submit_dop_generation — successful poll returns Completed result
    submit_dop_generation — Failed poll status returns failed result
    submit_dop_generation — timeout path returns timeout result
    submit_dop_generation — NSFW status raises HiggsFieldContentError
    upload_reference_image — 401 on presign raises HiggsFieldAuthError
    upload_reference_image — PUT failure raises HiggsFieldError
    upload_reference_image — success returns public_url
    PRICING dict — all three DoP tiers present and positive
    DOP_MODELS set — contains all three tiers
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.higgsfield import (
    DOP_MODELS,
    PRICING,
    HiggsFieldAuthError,
    HiggsFieldContentError,
    HiggsFieldError,
    HiggsFieldQuotaError,
    check_credentials,
    submit_dop_generation,
    upload_reference_image,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg_no_creds():
    cfg = MagicMock()
    cfg.HF_API_KEY = None
    cfg.HF_API_SECRET = None
    cfg.HIGGSFIELD_DRY_RUN = False
    cfg.HIGGSFIELD_TIMEOUT_SECONDS = 30
    cfg.HIGGSFIELD_POLL_INTERVAL_SECONDS = 1
    return cfg


@pytest.fixture()
def cfg_with_creds():
    cfg = MagicMock()
    cfg.HF_API_KEY = "test_key_id"
    cfg.HF_API_SECRET = "test_key_secret"
    cfg.HIGGSFIELD_DRY_RUN = False
    cfg.HIGGSFIELD_TIMEOUT_SECONDS = 30
    cfg.HIGGSFIELD_POLL_INTERVAL_SECONDS = 1
    return cfg


@pytest.fixture()
def cfg_dry_run():
    cfg = MagicMock()
    cfg.HF_API_KEY = "test_key_id"
    cfg.HF_API_SECRET = "test_key_secret"
    cfg.HIGGSFIELD_DRY_RUN = True
    cfg.HIGGSFIELD_TIMEOUT_SECONDS = 30
    cfg.HIGGSFIELD_POLL_INTERVAL_SECONDS = 1
    return cfg


# ---------------------------------------------------------------------------
# Static / schema tests
# ---------------------------------------------------------------------------

class TestPricingAndModels:

    def test_pricing_has_all_three_tiers(self):
        # API updated 2026-07-06: dop-preview replaced with dop-standard
        assert "dop-lite" in PRICING
        assert "dop-turbo" in PRICING
        assert "dop-standard" in PRICING

    def test_pricing_values_positive(self):
        for model, price in PRICING.items():
            assert price > 0, f"Price for {model} must be positive"

    def test_dop_models_set_contains_all_tiers(self):
        # API updated 2026-07-06: dop-preview replaced with dop-standard
        assert "dop-lite" in DOP_MODELS
        assert "dop-turbo" in DOP_MODELS
        assert "dop-standard" in DOP_MODELS

    def test_dop_lite_cheapest(self):
        assert PRICING["dop-lite"] < PRICING["dop-turbo"]
        assert PRICING["dop-lite"] < PRICING["dop-standard"]


# ---------------------------------------------------------------------------
# Credential checks
# ---------------------------------------------------------------------------

class TestCheckCredentials:

    def test_missing_key_returns_invalid(self, cfg_no_creds):
        result = check_credentials(cfg_no_creds)
        assert result["valid"] is False
        assert "HF_API_KEY" in result["detail"]

    def test_missing_secret_only(self):
        cfg = MagicMock()
        cfg.HF_API_KEY = "some_key"
        cfg.HF_API_SECRET = None
        result = check_credentials(cfg)
        assert result["valid"] is False

    def test_both_present_makes_api_request(self, cfg_with_creds):
        # check_credentials now uses POST /higgsfield-ai/dop/lite (not GET /requests).
        # The server rejects ALL GET requests with 405 regardless of auth — so GET
        # cannot distinguish valid from invalid credentials.
        # 422 = validation error on empty body = path exists + auth accepted.
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.json.return_value = {"detail": [{"msg": "Field required", "loc": ["body", "prompt"]}]}
        mock_resp.text = ""
        with patch("hypogeum.higgsfield.requests.post", return_value=mock_resp):
            result = check_credentials(cfg_with_creds)
        assert result["valid"] is True

    def test_401_returns_auth_rejected(self, cfg_with_creds):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        with patch("hypogeum.higgsfield.requests.post", return_value=mock_resp):
            result = check_credentials(cfg_with_creds)
        assert result["valid"] is False
        assert result["error"] == "auth_rejected"

    def test_422_treated_as_valid(self, cfg_with_creds):
        # 422 = auth passed, path exists, validation error on missing fields
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.json.return_value = {"detail": [{"msg": "Field required"}]}
        mock_resp.text = ""
        with patch("hypogeum.higgsfield.requests.post", return_value=mock_resp):
            result = check_credentials(cfg_with_creds)
        assert result["valid"] is True

    def test_network_error_returns_invalid(self, cfg_with_creds):
        import requests as _req
        with patch("hypogeum.higgsfield.requests.post",
                   side_effect=_req.ConnectionError("no route")):
            result = check_credentials(cfg_with_creds)
        assert result["valid"] is False
        assert result["error"] == "network_error"


# ---------------------------------------------------------------------------
# submit_dop_generation — dry run
# ---------------------------------------------------------------------------

class TestSubmitDopDryRun:

    def test_dry_run_flag_returns_immediately(self, cfg_with_creds):
        result = submit_dop_generation(
            cfg_with_creds,
            model="dop-lite",
            prompt="test prompt",
            image_url="https://example.com/img.jpg",
            dry_run=True,
        )
        assert result["status"] == "dry_run"
        assert result["dry_run"] is True
        assert result["model"] == "dop-lite"
        assert result["cost_usd"] == PRICING["dop-lite"]
        assert result["output_url"] is None

    def test_cfg_dry_run_also_skips(self, cfg_dry_run):
        result = submit_dop_generation(
            cfg_dry_run,
            model="dop-turbo",
            prompt="test",
            image_url="https://example.com/img.jpg",
        )
        assert result["status"] == "dry_run"

    def test_dry_run_returns_unique_request_ids(self, cfg_with_creds):
        r1 = submit_dop_generation(cfg_with_creds, model="dop-lite",
                                   prompt="p", image_url="u", dry_run=True)
        r2 = submit_dop_generation(cfg_with_creds, model="dop-lite",
                                   prompt="p", image_url="u", dry_run=True)
        assert r1["request_id"] != r2["request_id"]


# ---------------------------------------------------------------------------
# submit_dop_generation — model validation
# ---------------------------------------------------------------------------

class TestSubmitDopModelValidation:

    def test_unknown_model_raises(self, cfg_with_creds):
        with pytest.raises(HiggsFieldError, match="Unknown DoP model"):
            submit_dop_generation(
                cfg_with_creds,
                model="dop-unknown",
                prompt="p",
                image_url="u",
            )

    def test_all_valid_models_pass_dry_run(self, cfg_with_creds):
        for model in DOP_MODELS:
            result = submit_dop_generation(
                cfg_with_creds, model=model,
                prompt="test", image_url="https://example.com/img.jpg",
                dry_run=True,
            )
            assert result["model"] == model


# ---------------------------------------------------------------------------
# submit_dop_generation — HTTP error responses
# ---------------------------------------------------------------------------

class TestSubmitDopHttpErrors:

    def _mock_submit(self, status_code: int, body: dict = None):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = body or {}
        mock_resp.text = str(body or {})
        return mock_resp

    def test_401_raises_auth_error(self, cfg_with_creds):
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._mock_submit(401)):
            with pytest.raises(HiggsFieldAuthError):
                submit_dop_generation(
                    cfg_with_creds, model="dop-lite",
                    prompt="p", image_url="u",
                )

    def test_402_raises_quota_error(self, cfg_with_creds):
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._mock_submit(402)):
            with pytest.raises(HiggsFieldQuotaError):
                submit_dop_generation(
                    cfg_with_creds, model="dop-lite",
                    prompt="p", image_url="u",
                )

    def test_500_raises_higgsfield_error(self, cfg_with_creds):
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._mock_submit(500)):
            with pytest.raises(HiggsFieldError):
                submit_dop_generation(
                    cfg_with_creds, model="dop-lite",
                    prompt="p", image_url="u",
                )

    def test_400_raises_higgsfield_error(self, cfg_with_creds):
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._mock_submit(400)):
            with pytest.raises(HiggsFieldError):
                submit_dop_generation(
                    cfg_with_creds, model="dop-lite",
                    prompt="p", image_url="u",
                )


# ---------------------------------------------------------------------------
# submit_dop_generation — polling paths
# ---------------------------------------------------------------------------

class TestSubmitDopPolling:

    def _submit_resp(self, request_id: str = "req_test_123"):
        mock = MagicMock()
        mock.status_code = 200
        # Official API response (2026-07-06): request_id at top level
        # {"status":"queued","request_id":"...","status_url":"...","cancel_url":"..."}
        mock.json.return_value = {"status": "queued", "request_id": request_id,
                                  "status_url": f"https://platform.higgsfield.ai/requests/{request_id}/status",
                                  "cancel_url": f"https://platform.higgsfield.ai/requests/{request_id}/cancel"}
        mock.text = ""
        return mock

    def _poll_resp(self, status: str, output_url: str = None):
        mock = MagicMock()
        mock.status_code = 200
        # Real API statuses are lowercase: queued|in_progress|completed|failed|nsfw|cancelled
        body = {"status": status}
        if output_url:
            # Real API uses {"video": {"url": "..."}} for completed responses
            body["video"] = {"url": output_url}
        mock.json.return_value = body
        return mock

    def test_completed_poll_returns_result(self, cfg_with_creds):
        poll_resp = self._poll_resp("completed", "https://cdn.example.com/video.mp4")
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._submit_resp()), \
             patch("hypogeum.higgsfield.requests.get",
                   return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            result = submit_dop_generation(
                cfg_with_creds, model="dop-lite",
                prompt="test", image_url="https://example.com/img.jpg",
            )
        assert result["status"] == "Completed"
        assert result["output_url"] == "https://cdn.example.com/video.mp4"
        assert result["cost_usd"] == PRICING["dop-lite"]
        assert result["dry_run"] is False

    def test_failed_poll_returns_failed(self, cfg_with_creds):
        poll_resp = self._poll_resp("failed")
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._submit_resp()), \
             patch("hypogeum.higgsfield.requests.get",
                   return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            result = submit_dop_generation(
                cfg_with_creds, model="dop-lite",
                prompt="test", image_url="https://example.com/img.jpg",
            )
        assert result["status"] == "Failed"
        assert result["output_url"] is None
        assert result["cost_usd"] == 0.0  # failed = refunded

    def test_nsfw_poll_raises_content_error(self, cfg_with_creds):
        poll_resp = self._poll_resp("nsfw")
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._submit_resp()), \
             patch("hypogeum.higgsfield.requests.get",
                   return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            with pytest.raises(HiggsFieldContentError):
                submit_dop_generation(
                    cfg_with_creds, model="dop-lite",
                    prompt="test", image_url="https://example.com/img.jpg",
                )

    def test_cancelled_poll_returns_cancelled(self, cfg_with_creds):
        poll_resp = self._poll_resp("cancelled")
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._submit_resp()), \
             patch("hypogeum.higgsfield.requests.get",
                   return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            result = submit_dop_generation(
                cfg_with_creds, model="dop-lite",
                prompt="test", image_url="https://example.com/img.jpg",
            )
        assert result["status"] == "Cancelled"

    def test_timeout_returns_timeout(self, cfg_with_creds):
        # Set a very short timeout so the loop exits immediately
        cfg_with_creds.HIGGSFIELD_TIMEOUT_SECONDS = 0
        cfg_with_creds.HIGGSFIELD_POLL_INTERVAL_SECONDS = 0
        poll_resp = self._poll_resp("in_progress")
        with patch("hypogeum.higgsfield.requests.post",
                   return_value=self._submit_resp()), \
             patch("hypogeum.higgsfield.requests.get",
                   return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            result = submit_dop_generation(
                cfg_with_creds, model="dop-lite",
                prompt="test", image_url="https://example.com/img.jpg",
            )
        assert result["status"] == "timeout"

    def test_seed_included_in_payload(self, cfg_with_creds):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {"request_id": "req_seed_test"}
            mock.text = ""
            return mock

        poll_resp = self._poll_resp("Completed", "https://cdn.example.com/v.mp4")
        with patch("hypogeum.higgsfield.requests.post", side_effect=fake_post), \
             patch("hypogeum.higgsfield.requests.get", return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            submit_dop_generation(
                cfg_with_creds, model="dop-lite",
                prompt="test", image_url="https://example.com/img.jpg",
                seed=12345,
            )
        # New flat schema: seed is at top level, not nested under params
        assert captured["payload"]["seed"] == 12345

    def test_no_seed_omitted_from_payload(self, cfg_with_creds):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {"request_id": "req_noseed_test"}
            mock.text = ""
            return mock

        poll_resp = self._poll_resp("Completed", "https://cdn.example.com/v.mp4")
        with patch("hypogeum.higgsfield.requests.post", side_effect=fake_post), \
             patch("hypogeum.higgsfield.requests.get", return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            submit_dop_generation(
                cfg_with_creds, model="dop-lite",
                prompt="test", image_url="https://example.com/img.jpg",
                seed=None,
            )
        # New flat schema: seed should not be present at top level
        assert "seed" not in captured["payload"]

    def test_auth_header_format(self, cfg_with_creds):
        captured_headers = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured_headers.update(headers or {})
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {"request_id": "req_hdr_test"}
            mock.text = ""
            return mock

        poll_resp = self._poll_resp("Completed", "https://cdn.example.com/v.mp4")
        with patch("hypogeum.higgsfield.requests.post", side_effect=fake_post), \
             patch("hypogeum.higgsfield.requests.get", return_value=poll_resp), \
             patch("hypogeum.higgsfield.time.sleep"):
            submit_dop_generation(
                cfg_with_creds, model="dop-lite",
                prompt="test", image_url="https://example.com/img.jpg",
            )
        auth = captured_headers.get("Authorization", "")
        assert auth == "Key test_key_id:test_key_secret"


# ---------------------------------------------------------------------------
# upload_reference_image
# ---------------------------------------------------------------------------

class TestUploadReferenceImage:

    def test_401_on_presign_raises_auth_error(self, cfg_with_creds, tmp_path):
        img = tmp_path / "ref.jpg"
        img.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("hypogeum.higgsfield.requests.post", return_value=mock_resp):
            with pytest.raises(HiggsFieldAuthError):
                upload_reference_image(cfg_with_creds, img)

    def test_put_failure_raises_error(self, cfg_with_creds, tmp_path):
        img = tmp_path / "ref.jpg"
        img.write_bytes(b"\xff\xd8\xff")

        presign_resp = MagicMock()
        presign_resp.status_code = 200
        presign_resp.json.return_value = {
            "upload_url": "https://storage.example.com/put/here",
            "public_url": "https://cdn.example.com/ref.jpg",
        }

        put_resp = MagicMock()
        put_resp.status_code = 500

        with patch("hypogeum.higgsfield.requests.post", return_value=presign_resp), \
             patch("hypogeum.higgsfield.requests.put", return_value=put_resp):
            with pytest.raises(HiggsFieldError, match="PUT failed"):
                upload_reference_image(cfg_with_creds, img)

    def test_success_returns_public_url(self, cfg_with_creds, tmp_path):
        img = tmp_path / "ref.jpg"
        img.write_bytes(b"\xff\xd8\xff")

        presign_resp = MagicMock()
        presign_resp.status_code = 200
        presign_resp.json.return_value = {
            "upload_url": "https://storage.example.com/put/here",
            "public_url": "https://cdn.higgsfield.ai/ref.jpg",
        }

        put_resp = MagicMock()
        put_resp.status_code = 200

        with patch("hypogeum.higgsfield.requests.post", return_value=presign_resp), \
             patch("hypogeum.higgsfield.requests.put", return_value=put_resp):
            url = upload_reference_image(cfg_with_creds, img)

        assert url == "https://cdn.higgsfield.ai/ref.jpg"

    def test_presign_non_200_raises_error(self, cfg_with_creds, tmp_path):
        img = tmp_path / "ref.jpg"
        img.write_bytes(b"\xff\xd8\xff")

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "service unavailable"
        with patch("hypogeum.higgsfield.requests.post", return_value=mock_resp):
            with pytest.raises(HiggsFieldError, match="Failed to get upload URL"):
                upload_reference_image(cfg_with_creds, img)


# ---------------------------------------------------------------------------
# Comparison runner — dry-run smoke test
# ---------------------------------------------------------------------------

def _make_mock_cfg():
    """Build a MagicMock Config that satisfies all attributes the runner reads."""
    cfg = MagicMock()
    cfg.HF_API_KEY = None
    cfg.HF_API_SECRET = None
    cfg.HIGGSFIELD_DRY_RUN = True
    cfg.HIGGSFIELD_TIMEOUT_SECONDS = 30
    cfg.HIGGSFIELD_POLL_INTERVAL_SECONDS = 1
    cfg.MUAPI_API_KEY = None
    cfg.MUAPI_DRY_RUN = True
    cfg.MUAPI_BASE_URL = "https://api.muapi.ai/api/v1"
    cfg.MUAPI_TIMEOUT_SECONDS = 30
    cfg.MUAPI_POLL_INTERVAL_SECONDS = 1
    return cfg


class TestComparisonRunnerDryRun:

    def test_dry_run_produces_scorecard(self, tmp_path):
        """Smoke test: runner produces scorecard.json in dry-run mode."""
        from scripts.compare_higgsfield_vs_muapi import main
        with patch("scripts.compare_higgsfield_vs_muapi.Config", return_value=_make_mock_cfg()):
            rc = main([
                "--dry-run",
                "--legs", "muapi_t2v", "higgsfield_dop_lite",
                "--output-dir", str(tmp_path / "comparison_out"),
            ])
        assert rc == 0
        scorecard_path = tmp_path / "comparison_out" / "scorecard.json"
        assert scorecard_path.exists()
        sc = json.loads(scorecard_path.read_text())
        # legs_run moved under sc["summary"] in the updated scorecard structure
        assert sc["summary"]["legs_run"] == 2
        assert "results" in sc
        assert "recommendation" in sc

    def test_dry_run_all_results_dry(self, tmp_path):
        from scripts.compare_higgsfield_vs_muapi import main
        with patch("scripts.compare_higgsfield_vs_muapi.Config", return_value=_make_mock_cfg()):
            main([
                "--dry-run",
                "--legs", "muapi_t2v", "higgsfield_dop_lite",
                "--output-dir", str(tmp_path / "out"),
            ])
        sc = json.loads((tmp_path / "out" / "scorecard.json").read_text())
        for r in sc["results"]:
            assert r["dry_run"] is True

    def test_dry_run_summary_written(self, tmp_path):
        from scripts.compare_higgsfield_vs_muapi import main
        with patch("scripts.compare_higgsfield_vs_muapi.Config", return_value=_make_mock_cfg()):
            main([
                "--dry-run",
                "--legs", "muapi_t2v",
                "--output-dir", str(tmp_path / "out"),
            ])
        summary_path = tmp_path / "out" / "scorecard_summary.txt"
        assert summary_path.exists()
        text = summary_path.read_text()
        assert "SCORECARD" in text  # header updated to SEEDANCE 2 vs VEO3-FAST
        assert "RECOMMENDATION" in text

    def test_all_higgsfield_tiers_flag(self, tmp_path):
        from scripts.compare_higgsfield_vs_muapi import main
        with patch("scripts.compare_higgsfield_vs_muapi.Config", return_value=_make_mock_cfg()):
            main([
                "--dry-run",
                "--all-higgsfield-tiers",
                "--output-dir", str(tmp_path / "out"),
            ])
        sc = json.loads((tmp_path / "out" / "scorecard.json").read_text())
        leg_keys = {r["leg_key"] for r in sc["results"]}
        assert "higgsfield_dop_lite" in leg_keys
        assert "higgsfield_dop_turbo" in leg_keys
        assert "higgsfield_dop_preview" in leg_keys
