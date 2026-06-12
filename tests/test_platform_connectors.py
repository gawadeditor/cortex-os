"""
tests/test_platform_connectors.py — Platform Connector tests

Coverage:
    TestPublishRequestDefaults     — field defaults and build_publish_request
    TestYouTubeValidator           — title, description, tags, formats
    TestFacebookValidator          — title, description, video format
    TestInstagramValidator         — caption length, hashtag count, formats
    TestTikTokValidator            — title, hashtag limit, video format
    TestEmailValidator             — recipients required, subject, body, attachment count
    TestValidateRequest            — unknown platform, empty request, clean requests
    TestPlatformConnectorValidate  — connector.validate() returns correct status
    TestConnectorRegistry          — get_connector, get_supported_platforms
    TestBuildPublishRequest        — all platforms via helper
    TestNoNetworkCalls             — no sockets opened
    TestNoProductionSideEffects    — no ep_000 touched, no file writes
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from hypogeum.publishing.connectors import (
    PLATFORM_LIMITS,
    EmailConnector,
    FacebookConnector,
    InstagramConnector,
    PlatformConnector,
    PublishRequest,
    PublishResult,
    TikTokConnector,
    YouTubeConnector,
    build_publish_request,
    get_connector,
    get_supported_platforms,
    validate_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(platform, **kwargs) -> PublishRequest:
    defaults = {
        "episode_id": "ep_test",
        "title":      "Test Title",
    }
    defaults.update(kwargs)
    return PublishRequest(platform=platform, **defaults)


# ===========================================================================
# TestPublishRequestDefaults
# ===========================================================================

class TestPublishRequestDefaults:
    def test_default_empty_tags(self):
        r = PublishRequest(platform="youtube", episode_id="ep1")
        assert r.tags == []

    def test_default_empty_email_recipients(self):
        r = PublishRequest(platform="email", episode_id="ep1")
        assert r.email_recipients == []

    def test_default_metadata_empty(self):
        r = PublishRequest(platform="youtube", episode_id="ep1")
        assert r.metadata == {}

    def test_default_paths_none(self):
        r = PublishRequest(platform="youtube", episode_id="ep1")
        assert r.video_path is None
        assert r.thumbnail_path is None

    def test_build_publish_request_defaults(self):
        r = build_publish_request("youtube", "ep1")
        assert r.platform == "youtube"
        assert r.episode_id == "ep1"
        assert r.tags == []
        assert r.email_recipients == []

    def test_build_publish_request_lowercases_platform(self):
        r = build_publish_request("YouTube", "ep1")
        assert r.platform == "youtube"

    def test_build_publish_request_converts_path(self):
        r = build_publish_request("youtube", "ep1", video_path="/tmp/video.mp4")
        assert isinstance(r.video_path, Path)

    def test_build_publish_request_none_path_stays_none(self):
        r = build_publish_request("youtube", "ep1", video_path=None)
        assert r.video_path is None


# ===========================================================================
# TestYouTubeValidator
# ===========================================================================

class TestYouTubeValidator:
    def test_valid_request_no_errors(self):
        r = _req("youtube")
        assert validate_request("youtube", r) == []

    def test_missing_title(self):
        r = _req("youtube", title="")
        errs = validate_request("youtube", r)
        assert any("title is required" in e for e in errs)

    def test_title_too_long(self):
        r = _req("youtube", title="x" * 101)
        errs = validate_request("youtube", r)
        assert any("title" in e and "100" in e for e in errs)

    def test_title_at_limit_ok(self):
        r = _req("youtube", title="a" * 100)
        assert validate_request("youtube", r) == []

    def test_description_too_long(self):
        r = _req("youtube", description="d" * 5001)
        errs = validate_request("youtube", r)
        assert any("description" in e for e in errs)

    def test_description_at_limit_ok(self):
        r = _req("youtube", description="d" * 5000)
        assert validate_request("youtube", r) == []

    def test_too_many_tags(self):
        r = _req("youtube", tags=["tag"] * 501)
        errs = validate_request("youtube", r)
        assert any("tags" in e for e in errs)

    def test_valid_video_format(self):
        r = _req("youtube", video_path=Path("/tmp/clip.mp4"))
        assert validate_request("youtube", r) == []

    def test_invalid_video_format(self):
        r = _req("youtube", video_path=Path("/tmp/clip.xyz"))
        errs = validate_request("youtube", r)
        assert any("unsupported video format" in e for e in errs)

    def test_valid_thumbnail_format(self):
        r = _req("youtube", thumbnail_path=Path("/tmp/thumb.jpg"))
        assert validate_request("youtube", r) == []

    def test_invalid_thumbnail_format(self):
        r = _req("youtube", thumbnail_path=Path("/tmp/thumb.tiff"))
        errs = validate_request("youtube", r)
        assert any("thumbnail" in e for e in errs)

    def test_no_video_path_ok(self):
        r = _req("youtube")
        assert validate_request("youtube", r) == []


# ===========================================================================
# TestFacebookValidator
# ===========================================================================

class TestFacebookValidator:
    def test_valid_request_no_errors(self):
        r = _req("facebook")
        assert validate_request("facebook", r) == []

    def test_title_too_long(self):
        r = _req("facebook", title="x" * 256)
        errs = validate_request("facebook", r)
        assert any("title" in e for e in errs)

    def test_title_at_limit_ok(self):
        r = _req("facebook", title="x" * 255)
        assert validate_request("facebook", r) == []

    def test_description_too_long(self):
        r = _req("facebook", description="d" * 63207)
        errs = validate_request("facebook", r)
        assert any("description" in e for e in errs)

    def test_valid_video_format_mp4(self):
        r = _req("facebook", video_path=Path("/tmp/video.mp4"))
        assert validate_request("facebook", r) == []

    def test_invalid_video_format(self):
        r = _req("facebook", video_path=Path("/tmp/video.avi"))
        errs = validate_request("facebook", r)
        assert any("unsupported video format" in e for e in errs)


# ===========================================================================
# TestInstagramValidator
# ===========================================================================

class TestInstagramValidator:
    def test_valid_request_no_errors(self):
        r = _req("instagram")
        assert validate_request("instagram", r) == []

    def test_caption_too_long(self):
        r = _req("instagram", description="c" * 2201)
        errs = validate_request("instagram", r)
        assert any("caption" in e for e in errs)

    def test_caption_at_limit_ok(self):
        r = _req("instagram", description="c" * 2200)
        assert validate_request("instagram", r) == []

    def test_too_many_hashtags(self):
        tags = [f"#tag{i}" for i in range(31)]
        r = _req("instagram", tags=tags)
        errs = validate_request("instagram", r)
        assert any("hashtag" in e for e in errs)

    def test_hashtag_count_at_limit_ok(self):
        tags = [f"#tag{i}" for i in range(30)]
        r = _req("instagram", tags=tags)
        assert validate_request("instagram", r) == []

    def test_invalid_video_format(self):
        r = _req("instagram", video_path=Path("/tmp/video.avi"))
        errs = validate_request("instagram", r)
        assert any("unsupported video format" in e for e in errs)

    def test_valid_thumbnail_format(self):
        r = _req("instagram", thumbnail_path=Path("/tmp/thumb.jpg"))
        assert validate_request("instagram", r) == []

    def test_invalid_thumbnail_format(self):
        r = _req("instagram", thumbnail_path=Path("/tmp/thumb.gif"))
        errs = validate_request("instagram", r)
        assert any("thumbnail" in e for e in errs)

    def test_non_hashtag_tags_not_counted(self):
        # Only #-prefixed strings count toward the hashtag limit
        tags = ["plain_tag"] * 40   # no # prefix
        r = _req("instagram", tags=tags)
        assert validate_request("instagram", r) == []


# ===========================================================================
# TestTikTokValidator
# ===========================================================================

class TestTikTokValidator:
    def test_valid_request_no_errors(self):
        r = _req("tiktok")
        assert validate_request("tiktok", r) == []

    def test_title_too_long(self):
        r = _req("tiktok", title="x" * 151)
        errs = validate_request("tiktok", r)
        assert any("title" in e for e in errs)

    def test_title_at_limit_ok(self):
        r = _req("tiktok", title="x" * 150)
        assert validate_request("tiktok", r) == []

    def test_description_too_long(self):
        r = _req("tiktok", description="d" * 2201)
        errs = validate_request("tiktok", r)
        assert any("description" in e for e in errs)

    def test_too_many_hashtags(self):
        tags = [f"#tag{i}" for i in range(6)]
        r = _req("tiktok", tags=tags)
        errs = validate_request("tiktok", r)
        assert any("hashtag" in e for e in errs)

    def test_hashtag_count_at_limit_ok(self):
        tags = [f"#tag{i}" for i in range(5)]
        r = _req("tiktok", tags=tags)
        assert validate_request("tiktok", r) == []

    def test_invalid_video_format(self):
        r = _req("tiktok", video_path=Path("/tmp/video.avi"))
        errs = validate_request("tiktok", r)
        assert any("unsupported video format" in e for e in errs)

    def test_valid_video_format_mp4(self):
        r = _req("tiktok", video_path=Path("/tmp/clip.mp4"))
        assert validate_request("tiktok", r) == []


# ===========================================================================
# TestEmailValidator
# ===========================================================================

class TestEmailValidator:
    def test_valid_request_no_errors(self):
        r = _req("email",
                 email_recipients=["user@example.com"],
                 title="Subject line")
        assert validate_request("email", r) == []

    def test_missing_recipients(self):
        r = _req("email", email_recipients=[], title="Subject")
        errs = validate_request("email", r)
        assert any("recipient" in e for e in errs)

    def test_missing_subject(self):
        r = _req("email", email_recipients=["u@x.com"], title="")
        errs = validate_request("email", r)
        assert any("subject" in e for e in errs)

    def test_subject_too_long(self):
        r = _req("email",
                 email_recipients=["u@x.com"],
                 title="x" * 999)
        errs = validate_request("email", r)
        assert any("subject" in e for e in errs)

    def test_subject_at_limit_ok(self):
        r = _req("email",
                 email_recipients=["u@x.com"],
                 title="x" * 998)
        assert validate_request("email", r) == []

    def test_body_too_long(self):
        r = _req("email",
                 email_recipients=["u@x.com"],
                 title="Subj",
                 description="b" * 100_001)
        errs = validate_request("email", r)
        assert any("body" in e for e in errs)

    def test_multiple_recipients_ok(self):
        r = _req("email",
                 email_recipients=["a@x.com", "b@x.com", "c@x.com"],
                 title="Subj")
        assert validate_request("email", r) == []


# ===========================================================================
# TestValidateRequest
# ===========================================================================

class TestValidateRequest:
    def test_unknown_platform(self):
        r = _req("steam")
        errs = validate_request("steam", r)
        assert len(errs) == 1
        assert "unknown platform" in errs[0]

    def test_case_insensitive(self):
        r = _req("youtube", title="ok")
        r.platform = "YouTube"
        errs = validate_request("YouTube", r)
        assert errs == []

    def test_all_platforms_clean_request(self):
        platforms_reqs = {
            "youtube":   _req("youtube"),
            "facebook":  _req("facebook"),
            "instagram": _req("instagram"),
            "tiktok":    _req("tiktok"),
            "email":     _req("email",
                              email_recipients=["u@x.com"],
                              title="Subject"),
        }
        for plat, req in platforms_reqs.items():
            errs = validate_request(plat, req)
            assert errs == [], f"Unexpected errors for {plat}: {errs}"


# ===========================================================================
# TestPlatformConnectorValidate
# ===========================================================================

class TestPlatformConnectorValidate:
    def test_youtube_connector_valid_returns_validated(self):
        conn = YouTubeConnector()
        r = _req("youtube")
        result = conn.validate(r)
        assert isinstance(result, PublishResult)
        assert result.status == "validated"
        assert result.errors == []

    def test_youtube_connector_invalid_returns_invalid(self):
        conn = YouTubeConnector()
        r = _req("youtube", title="")  # missing title
        result = conn.validate(r)
        assert result.status == "invalid"
        assert len(result.errors) > 0

    def test_facebook_connector_valid(self):
        conn = FacebookConnector()
        result = conn.validate(_req("facebook"))
        assert result.status == "validated"

    def test_instagram_connector_valid(self):
        conn = InstagramConnector()
        result = conn.validate(_req("instagram"))
        assert result.status == "validated"

    def test_tiktok_connector_valid(self):
        conn = TikTokConnector()
        result = conn.validate(_req("tiktok"))
        assert result.status == "validated"

    def test_email_connector_valid(self):
        conn = EmailConnector()
        r = _req("email", email_recipients=["u@x.com"], title="Subj")
        result = conn.validate(r)
        assert result.status == "validated"

    def test_publish_raises_not_implemented(self):
        conn = YouTubeConnector()
        with pytest.raises(NotImplementedError):
            conn.publish(_req("youtube"))

    def test_result_episode_id_matches(self):
        conn = YouTubeConnector()
        r = _req("youtube", episode_id="ep_999")
        result = conn.validate(r)
        assert result.episode_id == "ep_999"

    def test_result_platform_matches(self):
        conn = TikTokConnector()
        r = _req("tiktok")
        result = conn.validate(r)
        assert result.platform == "tiktok"


# ===========================================================================
# TestConnectorRegistry
# ===========================================================================

class TestConnectorRegistry:
    def test_get_connector_youtube(self):
        assert isinstance(get_connector("youtube"), YouTubeConnector)

    def test_get_connector_facebook(self):
        assert isinstance(get_connector("facebook"), FacebookConnector)

    def test_get_connector_instagram(self):
        assert isinstance(get_connector("instagram"), InstagramConnector)

    def test_get_connector_tiktok(self):
        assert isinstance(get_connector("tiktok"), TikTokConnector)

    def test_get_connector_email(self):
        assert isinstance(get_connector("email"), EmailConnector)

    def test_get_connector_unknown_returns_none(self):
        assert get_connector("steam") is None

    def test_get_connector_case_insensitive(self):
        assert get_connector("YouTube") is not None

    def test_get_supported_platforms_sorted(self):
        platforms = get_supported_platforms()
        assert platforms == sorted(platforms)

    def test_get_supported_platforms_contains_all_five(self):
        platforms = set(get_supported_platforms())
        assert platforms == {"youtube", "facebook", "instagram", "tiktok", "email"}


# ===========================================================================
# TestBuildPublishRequest
# ===========================================================================

class TestBuildPublishRequest:
    def test_build_youtube(self):
        r = build_publish_request("youtube", "ep1", title="T", description="D")
        assert r.platform == "youtube"
        assert r.title == "T"
        assert r.description == "D"

    def test_build_email_with_recipients(self):
        r = build_publish_request(
            "email", "ep1",
            title="Subject",
            email_recipients=["a@b.com"],
        )
        assert r.email_recipients == ["a@b.com"]

    def test_build_with_tags(self):
        r = build_publish_request("tiktok", "ep1", tags=["#history"])
        assert "#history" in r.tags

    def test_build_with_channel_id(self):
        r = build_publish_request("youtube", "ep1", channel_id="yt_main")
        assert r.channel_id == "yt_main"

    def test_build_with_metadata(self):
        r = build_publish_request("youtube", "ep1", metadata={"key": "val"})
        assert r.metadata["key"] == "val"

    def test_build_with_paths(self):
        r = build_publish_request(
            "youtube", "ep1",
            video_path="/tmp/video.mp4",
            thumbnail_path="/tmp/thumb.jpg",
        )
        assert r.video_path == Path("/tmp/video.mp4")
        assert r.thumbnail_path == Path("/tmp/thumb.jpg")


# ===========================================================================
# TestPlatformLimits
# ===========================================================================

class TestPlatformLimits:
    def test_all_platforms_have_limits(self):
        for platform in ("youtube", "facebook", "instagram", "tiktok", "email"):
            assert platform in PLATFORM_LIMITS

    def test_youtube_title_limit(self):
        assert PLATFORM_LIMITS["youtube"]["title_max_chars"] == 100

    def test_instagram_hashtag_limit(self):
        assert PLATFORM_LIMITS["instagram"]["hashtags_max_count"] == 30

    def test_tiktok_hashtag_limit(self):
        assert PLATFORM_LIMITS["tiktok"]["hashtags_max_count"] == 5

    def test_email_no_required_video(self):
        assert PLATFORM_LIMITS["email"]["requires_video"] is False


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    def _block_socket(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise AssertionError("connectors opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)

    def test_validate_request_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        validate_request("youtube", _req("youtube"))

    def test_connector_validate_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        get_connector("youtube").validate(_req("youtube"))

    def test_build_publish_request_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        build_publish_request("tiktok", "ep1", title="T")

    def test_get_connector_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        get_connector("instagram")

    def test_all_validators_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        for platform in get_supported_platforms():
            if platform == "email":
                r = _req(platform, email_recipients=["u@x.com"], title="S")
            else:
                r = _req(platform)
            validate_request(platform, r)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_in_request(self):
        r = build_publish_request("youtube", "ep_test")
        assert r.episode_id != "ep_000"

    def test_validate_does_not_write_files(self, tmp_path):
        before = list(tmp_path.iterdir())
        validate_request("youtube", _req("youtube"))
        after = list(tmp_path.iterdir())
        assert before == after

    def test_connector_validate_does_not_write_files(self, tmp_path):
        before = list(tmp_path.iterdir())
        get_connector("tiktok").validate(_req("tiktok"))
        after = list(tmp_path.iterdir())
        assert before == after

    def test_publish_not_called_by_validate(self):
        """validate() must never call publish()."""
        conn = YouTubeConnector()
        called = []
        original = conn.publish

        def mock_publish(req):
            called.append(True)
            return original(req)

        conn.publish = mock_publish
        conn.validate(_req("youtube"))
        assert called == []
