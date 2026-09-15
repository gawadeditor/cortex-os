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
