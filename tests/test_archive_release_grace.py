"""tests/test_archive_release_grace.py — the archive sweep keeps an episode's files while its release is partial.
(Goes with the release-grace hook in hypogeum/archive_relocation.py.)"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import hypogeum.db as db
from tests.test_release_completeness import _intent, dbp, targets  # noqa: F401  (fixtures)


def test_archive_relocation_waits_for_an_incomplete_release_then_gives_up(dbp, monkeypatch, targets):
    """The sweep must not pick up an episode whose release is partial -- until the grace window is over."""
    from hypogeum import archive_relocation as ar
    from hypogeum.db import insert_archive_index_entry
    conn = db.get_connection(dbp)
    insert_archive_index_entry(conn, archive_id="g1", asset_id="x", episode_id="epg", asset_type="clip", provider="muapi",
                              archived_at="2026-08-01T00:00:00+00:00", file_path="episodes/epg/video/sc_s01.mp4")
    conn.commit()
    conn.close()
    fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
    _intent(dbp, "epg", "tiktok", "succeeded", fresh, ch="hypogeum")
    assert ar.find_pending_published_episodes(db_path=dbp) == []            # partial + fresh: keep the files
    conn = db.get_connection(dbp)
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    conn.execute("UPDATE publication_intents SET updated_at = ? WHERE episode_id = 'epg'", (old,))
    conn.commit()
    conn.close()
    assert ar.find_pending_published_episodes(db_path=dbp) == [("epg", "hypogeum")]   # grace over: archive anyway


def test_archive_relocation_moves_a_complete_release_at_once(dbp, monkeypatch, targets):
    from hypogeum import archive_relocation as ar
    from hypogeum.db import insert_archive_index_entry
    conn = db.get_connection(dbp)
    insert_archive_index_entry(conn, archive_id="g2", asset_id="y", episode_id="epc", asset_type="clip", provider="muapi",
                              archived_at="2026-08-01T00:00:00+00:00", file_path="episodes/epc/video/sc_s01.mp4")
    conn.commit()
    conn.close()
    for plat in ("facebook", "instagram", "tiktok", "youtube"):
        _intent(dbp, "epc", plat, "succeeded", datetime.now(timezone.utc) - timedelta(minutes=5), ch="hypogeum")
    assert ar.find_pending_published_episodes(db_path=dbp) == [("epc", "hypogeum")]
