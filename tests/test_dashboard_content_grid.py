"""Tests for hypogeum/dashboard_content_grid.py: a flaky Zernio refresh must
not blank out previously-good cached data, and a repeat fetch must not
re-probe an already-known thumbnail's orientation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum import dashboard_content_grid as cg


def _seed_post(db_path, *, channel_id, episode_id, post_id, deliverable_id="main_episode"):
    initialize_schema(db_path)
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    intent_id = f"intent_{post_id}"
    attempt_id = f"attempt_{post_id}"
    receipt_id = f"receipt_{post_id}"
    conn.execute(
        "INSERT INTO episodes (episode_id, title, created_at, status) VALUES (?, ?, ?, 'complete')",
        (episode_id, f"Title for {episode_id}", now),
    )
    conn.execute(
        """INSERT INTO publication_intents
           (publication_intent_id, channel_id, episode_id, deliverable_id, platform, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'youtube', 'succeeded', ?, ?)""",
        (intent_id, channel_id, episode_id, deliverable_id, now, now),
    )
    conn.execute(
        """INSERT INTO publishing_attempts
           (attempt_id, publication_intent_id, channel_id, episode_id, deliverable_id, platform,
            provider_id, state, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'youtube', 'zernio', 'succeeded', ?, ?)""",
        (attempt_id, intent_id, channel_id, episode_id, deliverable_id, now, now),
    )
    conn.execute(
        """INSERT INTO publishing_receipts
           (receipt_id, attempt_id, provider_id, platform, provider_publication_id, acknowledged_at)
           VALUES (?, ?, 'zernio', 'youtube', ?, ?)""",
        (receipt_id, attempt_id, post_id, now),
    )
    conn.commit()
    conn.close()


def _seed_cache_row(db_path, *, post_id, channel_id, episode_id, deliverable_id="main_episode",
                     age_seconds=2000, thumbnail_url="https://example.com/cover.jpg",
                     thumb_is_landscape=0):
    conn = get_connection(db_path)
    cached_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    conn.execute(
        """INSERT INTO content_grid_cache
           (post_id, channel_id, episode_id, deliverable_id, title, status, scheduled_for,
            published_at, thumbnail_url, thumb_is_landscape, platforms_json, fetch_error, cached_at)
           VALUES (?, ?, ?, ?, 'Real Title', 'published', NULL, '2026-09-01T10:00:00+00:00',
                   ?, ?, '[{"platform": "youtube", "status": "published"}]', NULL, ?)""",
        (post_id, channel_id, episode_id, deliverable_id, thumbnail_url, thumb_is_landscape, cached_at),
    )
    conn.commit()
    conn.close()


class _FailingProvider:
    """Simulates a Zernio outage: every get_post raises."""

    def get_post(self, post_id, *, db_path=None, timeout=None):
        raise RuntimeError("simulated Zernio outage")


class _CountingProvider:
    """Returns a post whose thumbnail is unchanged from what's cached, and
    records how many times it was called (used to check the orientation
    probe isn't re-run)."""

    def __init__(self, thumbnail_url):
        self.thumbnail_url = thumbnail_url
        self.calls = 0

    def get_post(self, post_id, *, db_path=None, timeout=None):
        self.calls += 1
        return {
            "status_code": 200,
            "post": {
                "title": "Refreshed Title",
                "status": "published",
                "scheduledFor": None,
                "mediaItems": [{"thumbnail": self.thumbnail_url}],
                "platforms": [{"platform": "youtube", "status": "published",
                                "platformPostUrl": "https://youtube.com/x",
                                "publishedAt": "2026-09-10T10:00:00+00:00"}],
            },
            "error": None,
        }


def test_fetch_error_preserves_last_known_good_data(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    _seed_post(db_path, channel_id="hypogeum", episode_id="ep_1", post_id="post_1")
    _seed_cache_row(db_path, post_id="post_1", channel_id="hypogeum", episode_id="ep_1")

    monkeypatch.setattr(
        "hypogeum.publishing.zernio.ZernioProvider", lambda: _FailingProvider()
    )

    items = cg.get_content_grid("hypogeum", db_path=db_path, force_refresh=True)

    assert len(items) == 1
    item = items[0]
    # The outage must surface as an error note, but must not blank the
    # last-known-good title/status/thumbnail/date that was already cached.
    assert item["fetch_error"] is not None
    assert item["display_title"] == "Real Title"
    assert item["status"] == "published"
    assert item["thumbnail_url"] == "https://example.com/cover.jpg"
    assert item["has_cover"] is True
    assert item["published_at"] == "2026-09-01T10:00:00+00:00"


def test_landscape_probe_skipped_when_thumbnail_unchanged(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    thumb = "https://example.com/cover.jpg"
    _seed_post(db_path, channel_id="hypogeum", episode_id="ep_1", post_id="post_1")
    _seed_cache_row(db_path, post_id="post_1", channel_id="hypogeum", episode_id="ep_1",
                     thumbnail_url=thumb, thumb_is_landscape=1)

    provider = _CountingProvider(thumb)
    monkeypatch.setattr("hypogeum.publishing.zernio.ZernioProvider", lambda: provider)

    probe_calls = {"n": 0}

    def _fake_is_landscape(url):
        probe_calls["n"] += 1
        return False

    monkeypatch.setattr(cg, "_is_landscape", _fake_is_landscape)

    items = cg.get_content_grid("hypogeum", db_path=db_path, force_refresh=True)

    assert len(items) == 1
    assert provider.calls == 1
    # thumbnail unchanged from cache -> orientation probe must be skipped,
    # and the previously-known value (landscape=True) must be reused.
    assert probe_calls["n"] == 0
    assert items[0]["is_landscape"] is True


def test_manually_resolved_cover_suppresses_warning_but_keeps_poster(tmp_path, monkeypatch):
    """Zernio's own record can never reflect a native, out-of-band platform
    edit (e.g. uploading a cover directly from YouTube Studio or a Facebook
    Page's own settings) -- once the operator confirms they've done that
    everywhere, the post must stop showing as "no cover" while still using
    the manual file as its poster, since that's the accurate cover now."""
    db_path = tmp_path / "test.db"
    _seed_post(db_path, channel_id="hypogeum", episode_id="ep_1", post_id="post_resolved")
    _seed_cache_row(db_path, post_id="post_resolved", channel_id="hypogeum", episode_id="ep_1",
                     age_seconds=10, thumbnail_url=None)

    monkeypatch.setitem(cg._MANUAL_COVER_FILES, "post_resolved", "ep_1")
    monkeypatch.setattr(cg, "_MANUALLY_COVER_RESOLVED", {"post_resolved"})

    items = cg.get_content_grid("hypogeum", db_path=db_path)

    assert len(items) == 1
    item = items[0]
    assert item["has_cover"] is True
    assert item["manual_cover_url"] == "/manual-cover/ep_1.jpg"
    assert "confirmed" in item["manual_cover_note"].lower()


def test_partial_status_flagged_needs_attention(tmp_path, monkeypatch):
    """Regression for the 2026-09-26 incident: a real post ("Pip the Tiny
    Star", twinkle_mini_tales) sat in Zernio's own 'partial' status with a
    wrong live YouTube thumbnail for hours before the operator caught it by
    chance -- Zernio's response already carried this signal the whole time,
    nothing on this page surfaced it. Any status other than a normal
    published/scheduled must come back flagged."""
    db_path = tmp_path / "test.db"
    _seed_post(db_path, channel_id="twinkle_mini_tales", episode_id="ep_1", post_id="post_partial")

    class _PartialProvider:
        def get_post(self, post_id, *, db_path=None, timeout=None):
            return {
                "status_code": 200,
                "post": {
                    "title": "Pip the Tiny Star",
                    "status": "partial",
                    "scheduledFor": None,
                    "mediaItems": [{"thumbnail": "https://example.com/real-cover.jpg"}],
                    "platforms": [
                        {"platform": "instagram", "status": "published",
                         "platformPostUrl": "https://instagram.com/x", "publishedAt": "2026-09-25T16:05:00+00:00"},
                        {"platform": "tiktok", "status": "published",
                         "platformPostUrl": "https://tiktok.com/x", "publishedAt": "2026-09-25T16:05:00+00:00"},
                        {"platform": "youtube", "status": "failed", "platformPostUrl": None, "publishedAt": None},
                        {"platform": "facebook", "status": "failed", "platformPostUrl": None, "publishedAt": None},
                    ],
                },
                "error": None,
            }

    monkeypatch.setattr("hypogeum.publishing.zernio.ZernioProvider", lambda: _PartialProvider())

    items = cg.get_content_grid("twinkle_mini_tales", db_path=db_path)

    assert len(items) == 1
    item = items[0]
    assert item["status"] == "partial"
    assert item["needs_attention"] is True
    assert set(item["not_yet_live_platforms"]) == {"youtube", "facebook"}


def test_published_status_not_flagged_needs_attention(tmp_path, monkeypatch):
    """A normal, fully-published post must never be flagged -- otherwise the
    signal is noise and gets ignored exactly like the thing it's meant to
    prevent."""
    db_path = tmp_path / "test.db"
    _seed_post(db_path, channel_id="hypogeum", episode_id="ep_1", post_id="post_1")
    _seed_cache_row(db_path, post_id="post_1", channel_id="hypogeum", episode_id="ep_1")

    items = cg.get_content_grid("hypogeum", db_path=db_path)

    assert len(items) == 1
    assert items[0]["status"] == "published"
    assert items[0]["needs_attention"] is False
    assert items[0]["not_yet_live_platforms"] == []


def test_manual_cover_pending_still_warns_until_resolved(tmp_path, monkeypatch):
    """The same post, before the operator confirms it's resolved, must
    still show the cover-ready-to-upload prompt and no_cover=False only
    once resolved."""
    db_path = tmp_path / "test.db"
    _seed_post(db_path, channel_id="hypogeum", episode_id="ep_1", post_id="post_pending")
    _seed_cache_row(db_path, post_id="post_pending", channel_id="hypogeum", episode_id="ep_1",
                     age_seconds=10, thumbnail_url=None)

    monkeypatch.setitem(cg._MANUAL_COVER_FILES, "post_pending", "ep_1")
    monkeypatch.setattr(cg, "_MANUALLY_COVER_RESOLVED", set())

    items = cg.get_content_grid("hypogeum", db_path=db_path)

    assert len(items) == 1
    item = items[0]
    assert item["has_cover"] is False
    assert item["manual_cover_url"] == "/manual-cover/ep_1.jpg"
    assert "confirmed" not in item["manual_cover_note"].lower()
