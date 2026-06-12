"""
tests/test_publishing_staging.py — Unit tests for hypogeum/publishing/staging.py

Tests:
  - UrlStagingProvider.stage_file() — success (JSON response), success (plain text)
  - UrlStagingProvider.stage_file() — HTTP error
  - UrlStagingProvider.stage_file() — network error
  - UrlStagingProvider.delete_staged_file() — success
  - UrlStagingProvider.delete_staged_file() — HTTP error (logged, not raised)
  - UrlStagingProvider.delete_staged_file() — network error (logged, not raised)
  - NullStagingProvider.stage_file() — raises StagingNotConfiguredError
  - NullStagingProvider.delete_staged_file() — raises StagingNotConfiguredError
  - get_staging_provider() factory — returns UrlStagingProvider when URL set
  - get_staging_provider() factory — returns NullStagingProvider when URL unset
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from hypogeum.publishing.staging import (
    NullStagingProvider,
    StagingError,
    StagingNotConfiguredError,
    UrlStagingProvider,
    get_staging_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status_code: int, text: str = "", json_data: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


# ---------------------------------------------------------------------------
# TestUrlStagingProviderStageFile
# ---------------------------------------------------------------------------

class TestUrlStagingProviderStageFile:
    def test_stage_file_json_url_response(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = UrlStagingProvider(base_url="https://staging.example.com", episode_id="ep_test")
        mock_resp = _make_response(200, json_data={"url": "https://staging.example.com/files/abc.mp4"})

        with patch("requests.post", return_value=mock_resp) as mock_post:
            url = provider.stage_file(video_file, "test.mp4")

        assert url == "https://staging.example.com/files/abc.mp4"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://staging.example.com/upload"

    def test_stage_file_plain_text_url_response(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = UrlStagingProvider(base_url="https://staging.example.com")
        mock_resp = _make_response(200, text="https://staging.example.com/files/plain.mp4")
        mock_resp.json.side_effect = ValueError("No JSON")

        with patch("requests.post", return_value=mock_resp):
            url = provider.stage_file(video_file, "test.mp4")

        assert url == "https://staging.example.com/files/plain.mp4"

    def test_stage_file_raises_on_http_error(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = UrlStagingProvider(base_url="https://staging.example.com")
        mock_resp = _make_response(500, text="Internal Server Error")

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(StagingError) as exc_info:
                provider.stage_file(video_file, "test.mp4")

        assert "500" in str(exc_info.value)

    def test_stage_file_raises_on_network_error(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = UrlStagingProvider(base_url="https://staging.example.com")

        with patch("requests.post", side_effect=requests.ConnectionError("conn error")):
            with pytest.raises(StagingError):
                provider.stage_file(video_file, "test.mp4")

    def test_stage_file_raises_when_response_has_no_valid_url(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = UrlStagingProvider(base_url="https://staging.example.com")
        mock_resp = _make_response(200, json_data={"url": ""})

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(StagingError):
                provider.stage_file(video_file, "test.mp4")

    def test_stage_file_raises_when_response_url_not_http(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = UrlStagingProvider(base_url="https://staging.example.com")
        mock_resp = _make_response(200, text="not-a-url")
        mock_resp.json.side_effect = ValueError("No JSON")

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(StagingError):
                provider.stage_file(video_file, "test.mp4")


# ---------------------------------------------------------------------------
# TestUrlStagingProviderDeleteStagedFile
# ---------------------------------------------------------------------------

class TestUrlStagingProviderDeleteStagedFile:
    def test_delete_succeeds(self):
        provider = UrlStagingProvider(base_url="https://staging.example.com")
        mock_resp = _make_response(200)

        with patch("requests.delete", return_value=mock_resp) as mock_del:
            provider.delete_staged_file("https://staging.example.com/files/abc.mp4")

        mock_del.assert_called_once_with(
            "https://staging.example.com/files/abc.mp4",
            timeout=30,
        )

    def test_delete_http_error_logged_not_raised(self):
        provider = UrlStagingProvider(base_url="https://staging.example.com")
        mock_resp = _make_response(404)

        # Should not raise even on 404
        with patch("requests.delete", return_value=mock_resp):
            provider.delete_staged_file("https://staging.example.com/files/abc.mp4")

    def test_delete_network_error_logged_not_raised(self):
        provider = UrlStagingProvider(base_url="https://staging.example.com")

        with patch("requests.delete", side_effect=requests.ConnectionError("no connection")):
            # Should not raise
            provider.delete_staged_file("https://staging.example.com/files/abc.mp4")


# ---------------------------------------------------------------------------
# TestNullStagingProvider
# ---------------------------------------------------------------------------

class TestNullStagingProvider:
    def test_stage_file_raises_immediately(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = NullStagingProvider()
        with pytest.raises(StagingNotConfiguredError):
            provider.stage_file(video_file, "test.mp4")

    def test_delete_staged_file_raises_immediately(self):
        provider = NullStagingProvider()
        with pytest.raises(StagingNotConfiguredError):
            provider.delete_staged_file("https://example.com/file.mp4")

    def test_error_message_mentions_configuration(self, tmp_path):
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video")

        provider = NullStagingProvider()
        with pytest.raises(StagingNotConfiguredError) as exc_info:
            provider.stage_file(video_file, "test.mp4")

        assert "META_STAGING_BASE_URL" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestGetStagingProvider
# ---------------------------------------------------------------------------

class TestGetStagingProvider:
    def test_returns_url_provider_when_url_set(self):
        provider = get_staging_provider("https://staging.example.com")
        assert isinstance(provider, UrlStagingProvider)

    def test_returns_null_provider_when_url_none(self):
        provider = get_staging_provider(None)
        assert isinstance(provider, NullStagingProvider)

    def test_returns_null_provider_when_url_empty_string(self):
        provider = get_staging_provider("")
        assert isinstance(provider, NullStagingProvider)

    def test_url_provider_has_correct_base_url(self):
        provider = get_staging_provider("https://staging.example.com/api")
        assert isinstance(provider, UrlStagingProvider)
        assert provider._base_url == "https://staging.example.com/api"

    def test_url_provider_strips_trailing_slash(self):
        provider = get_staging_provider("https://staging.example.com/")
        assert isinstance(provider, UrlStagingProvider)
        assert provider._base_url == "https://staging.example.com"
