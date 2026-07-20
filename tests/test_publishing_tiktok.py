"""
tests/test_publishing_tiktok.py — Unit tests for hypogeum/publishing/tiktok.py

Tests:
  - check_tiktok_availability: pass (200 from creator info), fail (401)
  - check_tiktok_availability: fail (credentials missing)
  - init_tiktok_upload: success returns (publish_id, upload_url)
  - init_tiktok_upload: hard failure on 400
  - upload_tiktok_chunks: success (single chunk)
  - upload_tiktok_chunks: hard failure on 403
  - poll_tiktok_publish: PUBLISH_COMPLETE returns post_id
  - poll_tiktok_publish: FAILED raises HardFailureError
  - poll_tiktok_publish: timeout raises PollTimeoutError
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from hypogeum.publishing.tiktok import (
    check_tiktok_availability,
    init_tiktok_upload,
    poll_tiktok_publish,
    upload_tiktok_chunks,
)
from hypogeum.publishing.retry import HardFailureError, PollTimeoutError, SoftFailureError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(
    status_code: int,
    json_data: dict | None = None,
    text: str = "",
    headers: dict | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = text
    resp.headers = headers or {}
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


def _make_config(has_credentials: bool = True):
    cfg = MagicMock()
    if has_credentials:
        cfg.TIKTOK_CLIENT_KEY = "tiktok_key_123"
        cfg.TIKTOK_CLIENT_SECRET = "tiktok_secret_123"
        cfg.TIKTOK_ACCESS_TOKEN = "tiktok_access_token_123"
        cfg.TIKTOK_OPEN_ID = "tiktok_open_id_123"
        cfg.tiktok_available = True
    else:
        cfg.TIKTOK_CLIENT_KEY = None
        cfg.TIKTOK_CLIENT_SECRET = None
        cfg.TIKTOK_ACCESS_TOKEN = None
        cfg.TIKTOK_OPEN_ID = None
        cfg.tiktok_available = False
    return cfg


def _fake_video(tmp_path: Path, size_bytes: int = 100) -> Path:
    p = tmp_path / "short.mp4"
    p.write_bytes(b"x" * size_bytes)
    return p


# ---------------------------------------------------------------------------
# TestCheckTiktokAvailability
# ---------------------------------------------------------------------------

class TestCheckTiktokAvailability:
    def test_returns_true_when_available(self):
        config = _make_config(has_credentials=True)
        mock_resp = _make_response(200, json_data={
            "data": {"creator_nickname": "TestCreator"},
            "error": {"code": "ok"}
        })

        with patch("requests.post", return_value=mock_resp):
            result = check_tiktok_availability(config, episode_id="ep_test")

        assert result is True

    def test_returns_false_when_credentials_missing(self):
        config = _make_config(has_credentials=False)

        result = check_tiktok_availability(config)
        assert result is False

    def test_returns_false_on_401(self):
        config = _make_config(has_credentials=True)
        mock_resp = _make_response(401, text="Unauthorized")

        with patch("requests.post", return_value=mock_resp):
            result = check_tiktok_availability(config)

        assert result is False

    def test_returns_false_on_network_error(self):
        config = _make_config(has_credentials=True)

        with patch("requests.post", side_effect=requests.ConnectionError("no connection")):
            result = check_tiktok_availability(config)

        assert result is False

    def test_returns_false_on_403(self):
        config = _make_config(has_credentials=True)
        mock_resp = _make_response(403, text="Forbidden")

        with patch("requests.post", return_value=mock_resp):
            result = check_tiktok_availability(config)

        assert result is False


# ---------------------------------------------------------------------------
# TestInitTiktokUpload
# ---------------------------------------------------------------------------

class TestInitTiktokUpload:
    def test_success_returns_publish_id_and_upload_url(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 200)

        mock_resp = _make_response(200, json_data={
            "data": {
                "publish_id": "tiktok_pub_abc123",
                "upload_url": "https://upload.tiktokapis.com/abc123",
            },
            "error": {"code": "ok"},
        })

        with patch("requests.post", return_value=mock_resp):
            publish_id, upload_url = init_tiktok_upload(
                config=config,
                video_path=video_path,
                title="My Short",
                episode_id="ep_test",
            )

        assert publish_id == "tiktok_pub_abc123"
        assert upload_url == "https://upload.tiktokapis.com/abc123"

    def test_400_raises_hard_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(400, json_data={"error": {"code": "invalid_params"}})

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                init_tiktok_upload(
                    config=config,
                    video_path=video_path,
                    title="My Short",
                )

        assert exc_info.value.status_code == 400

    def test_missing_publish_id_raises_soft_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200, json_data={
            "data": {"upload_url": "https://upload.tiktokapis.com/abc"},
        })

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(SoftFailureError):
                init_tiktok_upload(config=config, video_path=video_path, title="T")

    def test_http_success_empty_body_raises_controlled_soft_failure(self, tmp_path):
        """Real 2026-07-20 shape: HTTP status was ok but the body was empty —
        must never crash with an unhandled JSONDecodeError."""
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200, json_data=None, text="", headers={"Content-Type": "text/plain"})

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(SoftFailureError) as exc_info:
                init_tiktok_upload(config=config, video_path=video_path, title="T")

        message = str(exc_info.value)
        assert "200" in message
        assert "body_length=0" in message
        assert "text/plain" in message

    def test_http_success_invalid_json_raises_controlled_soft_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(
            200, json_data=None, text="<html>not json</html>",
            headers={"Content-Type": "text/html"},
        )

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(SoftFailureError) as exc_info:
                init_tiktok_upload(config=config, video_path=video_path, title="T")

        message = str(exc_info.value)
        assert "text/html" in message
        assert "<html>not json</html>" in message  # safe response-body prefix, not a secret

    def test_invalid_json_failure_never_leaks_secrets(self, tmp_path):
        config = _make_config()
        config.TIKTOK_ACCESS_TOKEN = "SUPER-SECRET-TIKTOK-TOKEN-xyz789"
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200, json_data=None, text="not json at all")

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(SoftFailureError) as exc_info:
                init_tiktok_upload(config=config, video_path=video_path, title="T")

        message = str(exc_info.value)
        assert "SUPER-SECRET-TIKTOK-TOKEN-xyz789" not in message
        assert "Authorization" not in message
        assert "Bearer" not in message

    def test_network_error_raises_soft_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        with patch("requests.post", side_effect=requests.ConnectionError("no conn")):
            with pytest.raises(SoftFailureError):
                init_tiktok_upload(config=config, video_path=video_path, title="T")

    def test_title_truncated_to_150_chars(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)
        long_title = "A" * 200

        mock_resp = _make_response(200, json_data={
            "data": {
                "publish_id": "pub_trunc",
                "upload_url": "https://upload.tiktokapis.com/trunc",
            }
        })

        with patch("requests.post", return_value=mock_resp) as mock_post:
            init_tiktok_upload(config=config, video_path=video_path, title=long_title)

        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json", {})
        assert len(body["post_info"]["title"]) == 150

    def test_default_privacy_level_is_public_to_everyone(self, tmp_path):
        """Default (no override) must preserve existing production behavior."""
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200, json_data={
            "data": {"publish_id": "pub_1", "upload_url": "https://upload.tiktokapis.com/1"}
        })

        with patch("requests.post", return_value=mock_resp) as mock_post:
            init_tiktok_upload(config=config, video_path=video_path, title="T")

        body = mock_post.call_args.kwargs.get("json", {})
        assert body["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"

    def test_explicit_self_only_privacy_level_is_sent(self, tmp_path):
        """Smoke-test path: explicit override must reach the request body."""
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200, json_data={
            "data": {"publish_id": "pub_2", "upload_url": "https://upload.tiktokapis.com/2"}
        })

        with patch("requests.post", return_value=mock_resp) as mock_post:
            init_tiktok_upload(
                config=config, video_path=video_path, title="T",
                privacy_level="SELF_ONLY",
            )

        body = mock_post.call_args.kwargs.get("json", {})
        assert body["post_info"]["privacy_level"] == "SELF_ONLY"

    def test_self_only_does_not_change_other_post_info_fields(self, tmp_path):
        """Overriding privacy_level must not disturb the other post_info flags."""
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200, json_data={
            "data": {"publish_id": "pub_3", "upload_url": "https://upload.tiktokapis.com/3"}
        })

        with patch("requests.post", return_value=mock_resp) as mock_post:
            init_tiktok_upload(
                config=config, video_path=video_path, title="My Short",
                privacy_level="SELF_ONLY",
            )

        post_info = mock_post.call_args.kwargs.get("json", {})["post_info"]
        assert post_info["disable_duet"] is False
        assert post_info["disable_comment"] is False
        assert post_info["disable_stitch"] is False
        assert post_info["title"] == "My Short"

    def test_only_one_init_request_regardless_of_privacy_level(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200, json_data={
            "data": {"publish_id": "pub_4", "upload_url": "https://upload.tiktokapis.com/4"}
        })

        with patch("requests.post", return_value=mock_resp) as mock_post:
            init_tiktok_upload(
                config=config, video_path=video_path, title="T",
                privacy_level="SELF_ONLY",
            )

        mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# TestUploadTiktokChunks
# ---------------------------------------------------------------------------

class TestUploadTiktokChunks:
    def test_single_chunk_upload_success(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(200)

        with patch("requests.put", return_value=mock_resp) as mock_put:
            upload_tiktok_chunks(
                config=config,
                upload_url="https://upload.tiktokapis.com/abc",
                video_path=video_path,
                episode_id="ep_test",
            )

        mock_put.assert_called_once()

    def test_403_raises_hard_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(403, json_data={"error": {"code": "forbidden"}})

        with patch("requests.put", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                upload_tiktok_chunks(
                    config=config,
                    upload_url="https://upload.tiktokapis.com/abc",
                    video_path=video_path,
                )

        assert exc_info.value.status_code == 403

    def test_network_error_raises_soft_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        with patch("requests.put", side_effect=requests.ConnectionError("err")):
            with pytest.raises(SoftFailureError):
                upload_tiktok_chunks(
                    config=config,
                    upload_url="https://upload.tiktokapis.com/abc",
                    video_path=video_path,
                )

    def test_sets_content_range_header(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 50)

        mock_resp = _make_response(200)

        with patch("requests.put", return_value=mock_resp) as mock_put:
            upload_tiktok_chunks(
                config=config,
                upload_url="https://upload.tiktokapis.com/abc",
                video_path=video_path,
            )

        call_kwargs = mock_put.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert "Content-Range" in headers
        assert headers["Content-Range"] == "bytes 0-49/50"


# ---------------------------------------------------------------------------
# TestPollTiktokPublish
# ---------------------------------------------------------------------------

class TestPollTiktokPublish:
    def test_publish_complete_returns_post_id(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={
            "data": {
                "status": "PUBLISH_COMPLETE",
                "publicaly_available_post_id": ["tt_post_abc123"],
            }
        })

        with patch("requests.post", return_value=mock_resp):
            post_id = poll_tiktok_publish(
                config=config,
                publish_id="tiktok_pub_abc",
                episode_id="ep_test",
                _sleep=lambda s: None,
            )

        assert post_id == "tt_post_abc123"

    def test_failed_status_raises_hard_failure(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={
            "data": {"status": "FAILED"}
        })

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                poll_tiktok_publish(
                    config=config,
                    publish_id="tiktok_pub_abc",
                    _sleep=lambda s: None,
                )

        assert exc_info.value.error_code == "FAILED"

    def test_poll_timeout_raises_poll_timeout_error(self):
        config = _make_config()

        # Always return PROCESSING_UPLOAD (non-terminal)
        mock_resp = _make_response(200, json_data={
            "data": {"status": "PROCESSING_UPLOAD"}
        })

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(PollTimeoutError):
                poll_tiktok_publish(
                    config=config,
                    publish_id="tiktok_pub_abc",
                    _sleep=lambda s: None,
                )

    def test_polls_multiple_times_before_complete(self):
        config = _make_config()

        processing = _make_response(200, json_data={"data": {"status": "PROCESSING_PUBLISH"}})
        complete = _make_response(200, json_data={
            "data": {
                "status": "PUBLISH_COMPLETE",
                "publicaly_available_post_id": ["post_after_wait"],
            }
        })

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                return processing
            return complete

        with patch("requests.post", side_effect=side_effect):
            post_id = poll_tiktok_publish(
                config=config,
                publish_id="tiktok_pub_abc",
                _sleep=lambda s: None,
            )

        assert post_id == "post_after_wait"
        assert call_count[0] == 3

    def test_falls_back_to_publish_id_when_no_post_id(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={
            "data": {"status": "PUBLISH_COMPLETE"}
        })

        with patch("requests.post", return_value=mock_resp):
            post_id = poll_tiktok_publish(
                config=config,
                publish_id="fallback_publish_id",
                _sleep=lambda s: None,
            )

        # Falls back to publish_id when no post_id in response
        assert post_id == "fallback_publish_id"
