"""
tests/test_publishing_meta.py — Unit tests for hypogeum/publishing/meta.py

Tests:
  - refresh_meta_token: success, 401 failure, network error
  - upload_facebook_video: three-phase chunked upload success
  - upload_facebook_video: retry on 500
  - upload_facebook_video: hard failure on 401
  - create_instagram_container: success
  - create_instagram_container: hard failure on 401
  - poll_instagram_container: FINISHED on first poll
  - poll_instagram_container: polls multiple times then FINISHED
  - poll_instagram_container: ERROR raises HardFailureError
  - poll_instagram_container: EXPIRED raises HardFailureError
  - publish_instagram_container: success returns media_id
  - publish_instagram_container: retry on 500
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from hypogeum.publishing.meta import (
    create_instagram_container,
    poll_instagram_container,
    publish_instagram_container,
    refresh_meta_token,
    upload_facebook_video,
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


def _make_config(staging_url: str = "https://staging.example.com"):
    cfg = MagicMock()
    cfg.META_APP_ID = "app_id_123"
    cfg.META_APP_SECRET = "app_secret_123"
    cfg.META_ACCESS_TOKEN = "EAA_access_token"
    cfg.META_INSTAGRAM_ACCOUNT_ID = "ig_account_123"
    cfg.META_FACEBOOK_PAGE_ID = "fb_page_123"
    cfg.META_GRAPH_API_VERSION = "v19.0"
    cfg.META_STAGING_BASE_URL = staging_url
    return cfg


def _fake_video(tmp_path: Path, size_bytes: int = 100) -> Path:
    p = tmp_path / "episode.mp4"
    p.write_bytes(b"x" * size_bytes)
    return p


# ---------------------------------------------------------------------------
# TestRefreshMetaToken
# ---------------------------------------------------------------------------

class TestRefreshMetaToken:
    def test_success_returns_new_token(self):
        config = _make_config()
        mock_resp = _make_response(200, json_data={"access_token": "EAA_new_long_lived"})

        with patch("requests.get", return_value=mock_resp):
            token = refresh_meta_token(config, episode_id="ep_test")

        assert token == "EAA_new_long_lived"

    def test_401_raises_hard_failure(self):
        config = _make_config()
        mock_resp = _make_response(401, text="Unauthorized")

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                refresh_meta_token(config)

        assert exc_info.value.status_code == 401

    def test_network_error_raises_soft_failure(self):
        config = _make_config()

        with patch("requests.get", side_effect=requests.ConnectionError("no conn")):
            with pytest.raises(SoftFailureError):
                refresh_meta_token(config)

    def test_missing_token_in_response_raises_hard_failure(self):
        config = _make_config()
        mock_resp = _make_response(200, json_data={"token_type": "bearer"})

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(HardFailureError):
                refresh_meta_token(config)


# ---------------------------------------------------------------------------
# TestUploadFacebookVideo
# ---------------------------------------------------------------------------

class TestUploadFacebookVideo:
    def test_three_phase_upload_success(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 150)

        start_resp = _make_response(200, json_data={
            "upload_session_id": "sess_abc",
            "start_offset": 0,
            "end_offset": 150,
        })

        transfer_resp = _make_response(200, json_data={
            "start_offset": 150,
            "end_offset": 150,
        })

        finish_resp = _make_response(200, json_data={"video_id": "fb_vid_123"})

        call_seq = iter([start_resp, transfer_resp, finish_resp])

        with patch("requests.post", side_effect=lambda *a, **k: next(call_seq)):
            video_id = upload_facebook_video(
                config=config,
                access_token="EAA_token",
                video_path=video_path,
                metadata={"title": "Test", "description": "Desc"},
                episode_id="ep_test",
            )

        assert video_id == "fb_vid_123"

    def test_401_raises_hard_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        mock_resp = _make_response(401, text="Unauthorized")

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                upload_facebook_video(
                    config=config,
                    access_token="EAA_token",
                    video_path=video_path,
                    metadata={"title": "T", "description": "D"},
                )

        assert exc_info.value.status_code == 401

    def test_500_on_start_raises_soft_failure(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        fail_resp = _make_response(500, text="Server Error")

        with patch("requests.post", return_value=fail_resp), \
             patch("hypogeum.publishing.retry.time.sleep"):
            with pytest.raises(SoftFailureError):
                upload_facebook_video(
                    config=config,
                    access_token="EAA_token",
                    video_path=video_path,
                    metadata={"title": "T", "description": "D"},
                )

    def test_uses_id_fallback_when_no_video_id(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 100)

        start_resp = _make_response(200, json_data={
            "upload_session_id": "sess_abc",
            "start_offset": 0,
            "end_offset": 100,
        })
        transfer_resp = _make_response(200, json_data={"start_offset": 100, "end_offset": 100})
        # finish response uses "id" instead of "video_id"
        finish_resp = _make_response(200, json_data={"id": "fb_vid_via_id"})

        seq = iter([start_resp, transfer_resp, finish_resp])
        with patch("requests.post", side_effect=lambda *a, **k: next(seq)):
            video_id = upload_facebook_video(
                config=config,
                access_token="EAA_token",
                video_path=video_path,
                metadata={"title": "T", "description": "D"},
            )

        assert video_id == "fb_vid_via_id"

    def test_default_published_true_preserves_existing_behavior(self, tmp_path):
        """No override -> existing production behavior (public) is unchanged."""
        config = _make_config()
        video_path = _fake_video(tmp_path, 150)

        start_resp = _make_response(200, json_data={
            "upload_session_id": "sess_pub", "start_offset": 0, "end_offset": 150,
        })
        transfer_resp = _make_response(200, json_data={"start_offset": 150, "end_offset": 150})
        finish_resp = _make_response(200, json_data={"video_id": "fb_vid_pub"})
        call_seq = iter([start_resp, transfer_resp, finish_resp])

        with patch("requests.post", side_effect=lambda *a, **k: next(call_seq)) as mock_post:
            upload_facebook_video(
                config=config,
                access_token="EAA_token",
                video_path=video_path,
                metadata={"title": "T", "description": "D"},
            )

        finish_call = mock_post.call_args_list[-1]
        assert finish_call.kwargs["params"]["published"] == "true"

    def test_explicit_published_false_is_sent_on_finish_call(self, tmp_path):
        """Smoke-test path: published=False must reach the Graph API finish call."""
        config = _make_config()
        video_path = _fake_video(tmp_path, 150)

        start_resp = _make_response(200, json_data={
            "upload_session_id": "sess_dark", "start_offset": 0, "end_offset": 150,
        })
        transfer_resp = _make_response(200, json_data={"start_offset": 150, "end_offset": 150})
        finish_resp = _make_response(200, json_data={"video_id": "fb_vid_dark"})
        call_seq = iter([start_resp, transfer_resp, finish_resp])

        with patch("requests.post", side_effect=lambda *a, **k: next(call_seq)) as mock_post:
            video_id = upload_facebook_video(
                config=config,
                access_token="EAA_token",
                video_path=video_path,
                metadata={"title": "T", "description": "D"},
                published=False,
            )

        finish_call = mock_post.call_args_list[-1]
        assert finish_call.kwargs["params"]["published"] == "false"
        # Real video ID is still returned/persisted even though unpublished.
        assert video_id == "fb_vid_dark"

    def test_published_false_does_not_alter_title_or_description(self, tmp_path):
        config = _make_config()
        video_path = _fake_video(tmp_path, 150)

        start_resp = _make_response(200, json_data={
            "upload_session_id": "sess_td", "start_offset": 0, "end_offset": 150,
        })
        transfer_resp = _make_response(200, json_data={"start_offset": 150, "end_offset": 150})
        finish_resp = _make_response(200, json_data={"video_id": "fb_vid_td"})
        call_seq = iter([start_resp, transfer_resp, finish_resp])

        with patch("requests.post", side_effect=lambda *a, **k: next(call_seq)) as mock_post:
            upload_facebook_video(
                config=config,
                access_token="EAA_token",
                video_path=video_path,
                metadata={"title": "Unchanged Title", "description": "Unchanged Desc"},
                published=False,
            )

        finish_call = mock_post.call_args_list[-1]
        assert finish_call.kwargs["params"]["title"] == "Unchanged Title"
        assert finish_call.kwargs["params"]["description"] == "Unchanged Desc"


# ---------------------------------------------------------------------------
# TestCreateInstagramContainer
# ---------------------------------------------------------------------------

class TestCreateInstagramContainer:
    def test_success_returns_creation_id(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={"id": "ig_container_abc"})

        with patch("requests.post", return_value=mock_resp):
            creation_id = create_instagram_container(
                config=config,
                access_token="EAA_token",
                video_url="https://staging.example.com/video.mp4",
                thumb_url="https://staging.example.com/thumb.jpg",
                caption="Test caption with #AI disclosure",
                episode_id="ep_test",
            )

        assert creation_id == "ig_container_abc"

    def test_401_raises_hard_failure(self):
        config = _make_config()

        mock_resp = _make_response(401, text="Unauthorized")

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                create_instagram_container(
                    config=config,
                    access_token="EAA_token",
                    video_url="https://staging.example.com/video.mp4",
                    thumb_url=None,
                    caption="Caption",
                )

        assert exc_info.value.status_code == 401

    def test_no_id_in_response_raises_soft_failure(self):
        config = _make_config()
        mock_resp = _make_response(200, json_data={"status": "ok"})

        with patch("requests.post", return_value=mock_resp), \
             patch("hypogeum.publishing.retry.time.sleep"):
            with pytest.raises(SoftFailureError):
                create_instagram_container(
                    config=config,
                    access_token="EAA_token",
                    video_url="https://example.com/v.mp4",
                    thumb_url=None,
                    caption="Cap",
                )


# ---------------------------------------------------------------------------
# TestPollInstagramContainer
# ---------------------------------------------------------------------------

class TestPollInstagramContainer:
    def test_finished_on_first_poll(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={"status_code": "FINISHED"})

        with patch("requests.get", return_value=mock_resp):
            status = poll_instagram_container(
                config=config,
                access_token="EAA_token",
                creation_id="ig_container_abc",
                episode_id="ep_test",
                _sleep=lambda s: None,
            )

        assert status == "FINISHED"

    def test_polls_multiple_times_then_finished(self):
        config = _make_config()

        in_progress = _make_response(200, json_data={"status_code": "IN_PROGRESS"})
        finished = _make_response(200, json_data={"status_code": "FINISHED"})

        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                return in_progress
            return finished

        with patch("requests.get", side_effect=side_effect):
            status = poll_instagram_container(
                config=config,
                access_token="EAA_token",
                creation_id="ig_container_abc",
                _sleep=lambda s: None,
            )

        assert status == "FINISHED"
        assert call_count[0] == 3

    def test_error_status_raises_hard_failure(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={"status_code": "ERROR"})

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                poll_instagram_container(
                    config=config,
                    access_token="EAA_token",
                    creation_id="ig_container_abc",
                    _sleep=lambda s: None,
                )

        assert exc_info.value.error_code == "ERROR"

    def test_expired_status_raises_hard_failure(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={"status_code": "EXPIRED"})

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                poll_instagram_container(
                    config=config,
                    access_token="EAA_token",
                    creation_id="ig_container_abc",
                    _sleep=lambda s: None,
                )

        assert exc_info.value.error_code == "EXPIRED"

    def test_poll_timeout_raises_poll_timeout_error(self):
        config = _make_config()

        in_progress = _make_response(200, json_data={"status_code": "IN_PROGRESS"})

        with patch("requests.get", return_value=in_progress):
            with pytest.raises(PollTimeoutError):
                poll_instagram_container(
                    config=config,
                    access_token="EAA_token",
                    creation_id="ig_container_abc",
                    _sleep=lambda s: None,
                )


# ---------------------------------------------------------------------------
# TestPublishInstagramContainer
# ---------------------------------------------------------------------------

class TestPublishInstagramContainer:
    def test_success_returns_media_id(self):
        config = _make_config()

        mock_resp = _make_response(200, json_data={"id": "ig_media_xyz"})

        with patch("requests.post", return_value=mock_resp):
            media_id = publish_instagram_container(
                config=config,
                access_token="EAA_token",
                creation_id="ig_container_abc",
                episode_id="ep_test",
            )

        assert media_id == "ig_media_xyz"

    def test_401_raises_hard_failure(self):
        config = _make_config()

        mock_resp = _make_response(401, text="Unauthorized")

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(HardFailureError) as exc_info:
                publish_instagram_container(
                    config=config,
                    access_token="EAA_token",
                    creation_id="ig_container_abc",
                )

        assert exc_info.value.status_code == 401

    def test_500_triggers_retry(self):
        config = _make_config()

        fail_resp = _make_response(500, text="Server Error")
        success_resp = _make_response(200, json_data={"id": "ig_media_after_retry"})

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return fail_resp
            return success_resp

        with patch("requests.post", side_effect=side_effect), \
             patch("hypogeum.publishing.retry.time.sleep"):
            media_id = publish_instagram_container(
                config=config,
                access_token="EAA_token",
                creation_id="ig_container_abc",
            )

        assert media_id == "ig_media_after_retry"
        assert call_count[0] == 2
