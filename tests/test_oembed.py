"""Tests for hypogeum.oembed — URL → platform/post-id resolution."""
from __future__ import annotations

import pytest

from hypogeum import oembed


@pytest.mark.parametrize("url,platform,pid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/abc123DEF45", "youtube", "abc123DEF45"),
    ("https://www.tiktok.com/@acme/video/7291234567890123456", "tiktok", "7291234567890123456"),
    ("https://www.instagram.com/reel/CxyZ_abc12/", "instagram", "CxyZ_abc12"),
    ("https://www.facebook.com/watch/?v=1234567890", "facebook", "1234567890"),
])
def test_id_resolution_offline(url, platform, pid, monkeypatch):
    # No network: force the oembed call to fail so we exercise URL parsing only.
    monkeypatch.setattr(oembed, "_OEMBED", {})
    r = oembed.resolve_url(url)
    assert r["platform"] == platform
    assert r["platform_post_id"] == pid
    assert r["ok"] is True


def test_unrecognised_url():
    r = oembed.resolve_url("https://example.com/some/post")
    assert r["ok"] is False and r["platform"] is None


def test_not_a_url():
    r = oembed.resolve_url("just text")
    assert r["ok"] is False and "URL" in r["error"]


def test_oembed_metadata_merged(monkeypatch):
    class _Resp:
        status_code = 200
        def json(self):
            return {"title": "My Video", "author_name": "Acme",
                    "thumbnail_url": "http://t/x.jpg"}

    class _Requests:
        @staticmethod
        def get(*a, **k):
            return _Resp()

    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)
    r = oembed.resolve_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert r["title"] == "My Video" and r["thumbnail_url"] == "http://t/x.jpg"
    assert r["ok"] is True
